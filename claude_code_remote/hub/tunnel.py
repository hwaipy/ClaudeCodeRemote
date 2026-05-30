"""反向 tunnel server — App ↔ Hub 长连 WS (ccr-hub-spec.html §2).

App 端用 device_token query-param 连进来. Hub:
1. 校验 token → 找 app
2. 等 control op=hello, 回 control op=ready
3. 加入 online registry, 后续帧靠这条 WS 流转

M-Hub-0 范围: 只跑通握手 + registry; HTTP/WS forward 在 M-Hub-1 加.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..shared import tunnel_proto as tp
from . import db as hub_db

log = logging.getLogger("ccr.hub.tunnel")

router = APIRouter()


class _WsStream:
    """一个 forwarded user WebSocket 的 hub 侧端点.

    - inbound: 从 app (经反向 WS) 收到的 WsMsg 帧 → push 给 user_ws.send
    - outbound: user_ws.receive → 转 WsMsg 帧给 app
    - close: 任一方关闭 → 发 WsClose 给对端 + 关 user_ws
    """
    __slots__ = ("stream_id", "user_ws", "inbound", "closed")

    def __init__(self, stream_id: str, user_ws: WebSocket) -> None:
        self.stream_id = stream_id
        self.user_ws = user_ws
        # inbound queue 元素: WsMsg | WsClose
        self.inbound: asyncio.Queue[tp.AnyFrame] = asyncio.Queue()
        self.closed = False


class _OnlineApp:
    """一个 connected app 的运行时状态.

    pending_http: stream_id -> Future[HttpRes].
    ws_streams:   stream_id -> _WsStream  (active forwarded ws)
    """
    __slots__ = ("app_id", "user_id", "name", "ws", "connected_at",
                 "version", "capabilities",
                 "pending_http", "ws_streams", "_send_lock")

    def __init__(self, app_id: str, user_id: str, name: str,
                 ws: WebSocket, version: str = "",
                 capabilities: list[str] | None = None) -> None:
        self.app_id = app_id
        self.user_id = user_id
        self.name = name
        self.ws = ws
        self.connected_at = time.time()
        self.version = version
        self.capabilities = capabilities or []
        self.pending_http: dict[str, asyncio.Future[tp.HttpRes]] = {}
        self.ws_streams: dict[str, _WsStream] = {}
        # FastAPI WebSocket.send_text 不是协程安全, 多并发 forward 要 lock 串行化
        self._send_lock = asyncio.Lock()

    async def send_frame(self, frame: tp.AnyFrame) -> None:
        async with self._send_lock:
            await self.ws.send_text(tp.encode(frame))

    async def send_http_req(self, req: tp.HttpReq, timeout: float = 30.0,
                            ) -> tp.HttpRes:
        """发请求, 等响应 (按 req.stream_id 配对). 超时抛 asyncio.TimeoutError."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tp.HttpRes] = loop.create_future()
        self.pending_http[req.stream_id] = fut
        try:
            await self.send_frame(req)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending_http.pop(req.stream_id, None)

    def resolve_http_res(self, res: tp.HttpRes) -> bool:
        fut = self.pending_http.get(res.stream_id)
        if fut and not fut.done():
            fut.set_result(res)
            return True
        return False

    def register_ws_stream(self, stream: _WsStream) -> None:
        self.ws_streams[stream.stream_id] = stream

    def get_ws_stream(self, stream_id: str) -> _WsStream | None:
        return self.ws_streams.get(stream_id)

    def remove_ws_stream(self, stream_id: str) -> None:
        s = self.ws_streams.pop(stream_id, None)
        if s:
            s.closed = True

    def fail_all_pending(self, exc: Exception) -> None:
        for fut in list(self.pending_http.values()):
            if not fut.done():
                fut.set_exception(exc)
        self.pending_http.clear()
        # ws streams: 给每个 inbound queue 推个 disconnect 让 forward task 结束
        for s in list(self.ws_streams.values()):
            try:
                s.inbound.put_nowait(
                    tp.WsClose(stream_id=s.stream_id, code=1011,
                                reason="app_offline"),
                )
            except Exception:
                pass
            s.closed = True
        self.ws_streams.clear()


class _Registry:
    """全局: app_id → _OnlineApp. 多进程后要改成 redis / 别的."""

    def __init__(self) -> None:
        self._apps: dict[str, _OnlineApp] = {}
        self._lock = asyncio.Lock()

    async def add(self, app: _OnlineApp) -> None:
        async with self._lock:
            # 已有同 app_id 旧连接? 踢它.
            old = self._apps.get(app.app_id)
            if old:
                try:
                    await old.ws.close(code=1012, reason="superseded")
                except Exception:
                    pass
            self._apps[app.app_id] = app

    async def remove(self, app_id: str, ws: WebSocket) -> None:
        async with self._lock:
            cur = self._apps.get(app_id)
            if cur and cur.ws is ws:
                self._apps.pop(app_id, None)

    def get(self, app_id: str) -> _OnlineApp | None:
        return self._apps.get(app_id)

    def online_apps(self) -> list[_OnlineApp]:
        return list(self._apps.values())

    def online_app_ids(self) -> list[str]:
        return list(self._apps.keys())

    async def disconnect_app(self, app_id: str) -> bool:
        """主动关掉指定 app 的反向 WS (revoke 时调). 找不到也算 OK."""
        async with self._lock:
            app = self._apps.pop(app_id, None)
        if app is None:
            return False
        try:
            await app.ws.close(code=1008, reason="revoked")
        except Exception:
            pass
        return True


registry = _Registry()


async def _handle_app_control(online: "_OnlineApp", frame: tp.Control) -> None:
    """App → Hub control 帧 dispatch (M-Hub-2 metadata sync)."""
    op = frame.op
    if op == "sessions_snapshot":
        # 全量覆盖: 先清该 app 旧 cache, 再 upsert 当前所有 sessions
        sessions = frame.data.get("sessions") or []
        await hub_db.clear_app_sessions(online.app_id)
        for s in sessions:
            sid = s.get("id")
            if not sid:
                continue
            await hub_db.upsert_session(
                online.app_id, online.user_id, sid, _norm_session(s),
            )
    elif op == "session_state":
        # delta upsert. data 含 status_payload 字段 (id, name, cwd, state, ...)
        sid = frame.data.get("id")
        if sid:
            await hub_db.upsert_session(
                online.app_id, online.user_id, sid,
                _norm_session(frame.data),
            )
    elif op == "session_removed":
        sid = frame.data.get("id")
        if sid:
            await hub_db.remove_session(online.app_id, sid)
    elif op == "session_touch":
        sid = frame.data.get("id")
        la = frame.data.get("last_active")
        if sid and la is not None:
            await hub_db.upsert_session(
                online.app_id, online.user_id, sid,
                {"last_active": la},
            )
    else:
        log.debug("unhandled control op=%s from %s", op, online.app_id)


def _norm_session(payload: dict) -> dict:
    """status_payload → sessions_cache fields. 只挑能写入的列."""
    out = {}
    if "name" in payload: out["name"] = payload.get("name") or ""
    if "cwd" in payload: out["cwd"] = payload.get("cwd") or ""
    if "state" in payload: out["state"] = payload.get("state")
    if "model" in payload: out["model"] = payload.get("model") or ""
    if "effort" in payload: out["effort"] = payload.get("effort") or ""
    if "permission_mode" in payload:
        out["permission_mode"] = payload.get("permission_mode")
    # status_payload 命名约定: last_activity_at; spec/db 命名: last_active
    if "last_activity_at" in payload:
        out["last_active"] = payload.get("last_activity_at")
    elif "last_active" in payload:
        out["last_active"] = payload.get("last_active")
    if "created_at" in payload:
        out["created_at"] = payload.get("created_at")
    # M-Hub-2+: stash / inactive / pending_perm 也透传 — SPA home view 按这些
    # 分区到 Active / Stash / Inactive section, 显示 ▲badge 数等.
    if "is_stash" in payload:
        out["is_stash"] = bool(payload.get("is_stash"))
    if "is_inactive" in payload:
        out["is_inactive"] = bool(payload.get("is_inactive"))
    if "pending_permissions" in payload:
        out["pending_permissions"] = int(payload.get("pending_permissions") or 0)
    if "needs_action_detail" in payload:
        out["needs_action_detail"] = payload.get("needs_action_detail")
    # cur_model: claude CLI 报回的实际 model id (e.g. deepseek-v4-pro 走 USTC,
    # 或 claude-opus-4-7-...). SPA 用它判断该 session 是 native claude 还是
    # 自定义 endpoint 来决定 model 选择 UI.
    if "cur_model" in payload:
        out["cur_model"] = payload.get("cur_model") or ""
    # seen_at: server 端维护的已读基线. SPA 用 (state idle/finished 且
    # last_active > seen_at) 判未读蓝点. 跨设备一致 (所有设备经 hub).
    if "seen_at" in payload:
        out["seen_at"] = payload.get("seen_at")
    return out


async def _safe_close(ws: WebSocket, code: int, reason: str = "") -> None:
    if ws.client_state == WebSocketState.DISCONNECTED:
        return
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


@router.websocket("/app-tunnel")
async def app_tunnel(
    ws: WebSocket,
    token: str = Query(...),
):
    """App 端连进来的反向 WS endpoint.

    流程:
      ① accept ws
      ② 校验 token → 找 app 记录
      ③ 等 hello 帧 (5s 超时); 校验 version
      ④ 发 ready 帧, 加入 registry, 标 last_seen
      ⑤ 进入 read loop — 收 ping → 回 pong; 其它帧 M-Hub-0 暂忽略
    """
    await ws.accept()

    row = await hub_db.find_app_by_token(token)
    if not row:
        await _safe_close(ws, code=1008, reason="bad_token")
        return

    # 等 hello (5s)
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=5)
    except asyncio.TimeoutError:
        await _safe_close(ws, code=1008, reason="hello_timeout")
        return
    except WebSocketDisconnect:
        return

    try:
        hello = tp.decode(raw)
    except Exception:
        await _safe_close(ws, code=1003, reason="bad_hello_format")
        return

    if not isinstance(hello, tp.Control) or hello.op != "hello":
        await _safe_close(ws, code=1003, reason="expected_hello")
        return

    version = str(hello.data.get("version", ""))
    if version and version != tp.TUNNEL_PROTO_VERSION:
        await _safe_close(ws, code=1008, reason=f"version_mismatch:{version}")
        return

    online = _OnlineApp(
        app_id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        ws=ws,
        version=version,
        capabilities=list(hello.data.get("capabilities") or []),
    )
    await registry.add(online)
    await hub_db.touch_app_seen(online.app_id)
    log.info("app online: %s (%s) user=%s",
             online.app_id, online.name, online.user_id)

    # ready — 带 short_host 给 app 端拼公开 share URL (spec §17).
    # row 由 find_app_by_token 返回, 内部已 backfill short_host.
    short_host = row.get("short_host") or ""
    ready = tp.Control(
        stream_id="*",
        op="ready",
        data={
            "app_id": online.app_id,
            "user_id": online.user_id,
            "short_host": short_host,
        },
    )
    await ws.send_text(tp.encode(ready))

    # main loop
    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = tp.decode(raw)
            except Exception:
                log.warning("bad frame from %s: %r", online.app_id, raw[:200])
                continue

            if isinstance(frame, tp.Ping):
                await online.send_frame(tp.Pong(stream_id=frame.stream_id))
            elif isinstance(frame, tp.HttpRes):
                if not online.resolve_http_res(frame):
                    log.warning("orphan http_res stream_id=%s from %s",
                                frame.stream_id, online.app_id)
            elif isinstance(frame, (tp.WsMsg, tp.WsClose)):
                stream = online.get_ws_stream(frame.stream_id)
                if stream is None:
                    log.debug("orphan ws frame stream_id=%s type=%s",
                              frame.stream_id, frame.type)
                else:
                    try:
                        stream.inbound.put_nowait(frame)
                    except Exception:
                        log.exception("ws inbound enqueue failed")
            elif isinstance(frame, tp.Control):
                await _handle_app_control(online, frame)
            else:
                log.debug("frame type=%s unhandled",
                          getattr(frame, "type", "?"))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("tunnel loop error for app %s", online.app_id)
    finally:
        online.fail_all_pending(
            RuntimeError(f"app {online.app_id} disconnected"),
        )
        # 累加这次在线时长到 db
        elapsed = max(0.0, time.time() - online.connected_at)
        try:
            await hub_db.bump_app_online_time(online.app_id, elapsed)
        except Exception:
            log.exception("bump_app_online_time failed for %s", online.app_id)
        await registry.remove(online.app_id, ws)
        log.info("app offline: %s (online %.0fs)", online.app_id, elapsed)
