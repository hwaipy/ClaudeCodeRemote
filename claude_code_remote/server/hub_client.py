"""App 端: 反向 WS client (ccr-hub-spec.html §6).

启动时连 Hub, 维持长连. 收到 http_req 帧 → ASGI in-process 调本进程 FastAPI app
→ 回 http_res. 断线指数退避重连. 没配 hub_url 时整个模块 noop, 纯本地零开销.

约定 env:
  CCR_HUB_URL          e.g. ws://hub.example.com  (空 → 不连)
  CCR_HUB_DEVICE_TOKEN 配对后由 ccr pair 写
  CCR_HUB_APP_NAME     display name (默认 hostname)

App 启动时调 hub_client.start(app, env). 不 block, 后台 task 跑.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket

import websockets

from ..shared import tunnel_proto as tp

log = logging.getLogger("ccr.hub_client")


def _ws_url(base: str) -> str:
    """http://x → ws://x/app-tunnel. ws://x → ws://x/app-tunnel."""
    base = base.rstrip("/")
    if base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    return base + "/app-tunnel"


class HubClient:
    """单实例. start() 后后台跑 connect loop, 直到 stop()."""

    def __init__(self, asgi_app, hub_url: str, device_token: str,
                 app_name: str) -> None:
        self.asgi_app = asgi_app
        self.hub_url = hub_url
        self.device_token = device_token
        self.app_name = app_name
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self.app_id: str | None = None
        self.user_id: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="hub_client")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 1.0   # 成功一轮后重置退避
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("hub_client disconnected: %s; retry in %.1fs",
                         e, backoff)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 1.7, 30.0)

    async def _connect_once(self) -> None:
        url = _ws_url(self.hub_url) + f"?token={self.device_token}"
        log.info("hub_client connecting %s", url)
        # max_size 默认 1 MiB — claude 的大 tool_result / 大 diff 经 base64
        # 编码 (+33%) 经常超 1MB → 1009 message too big → 整条 tunnel 断开.
        # 调到 64 MiB 兜底 (跟 hub 端 starlette WebSocket 默认对齐).
        async with websockets.connect(url, ping_interval=20,
                                       ping_timeout=15,
                                       max_size=64 * 1024 * 1024) as ws:
            # hello
            hello = tp.Control(
                stream_id="*",
                op="hello",
                data={
                    "app_name": self.app_name,
                    "version": tp.TUNNEL_PROTO_VERSION,
                    "capabilities": [],
                },
            )
            await ws.send(tp.encode(hello))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            ready = tp.decode(raw)
            if not isinstance(ready, tp.Control) or ready.op != "ready":
                raise RuntimeError(f"unexpected ready frame: {ready!r}")
            self.app_id = ready.data.get("app_id")
            self.user_id = ready.data.get("user_id")
            self.connected.set()
            log.info("hub_client ready app_id=%s user_id=%s",
                     self.app_id, self.user_id)

            # M-Hub-2: 启 metadata pump task — 订阅 session_manager 的
            # global stream, 翻成 control 帧推 hub.
            meta_task = asyncio.create_task(
                self._metadata_pump(ws), name="hub_metadata_pump",
            )
            try:
                await self._loop(ws)
            finally:
                meta_task.cancel()
                try:
                    await meta_task
                except (asyncio.CancelledError, Exception):
                    pass
                self.connected.clear()
                self.app_id = None
                self.user_id = None

    async def _loop(self, ws) -> None:
        send_lock = asyncio.Lock()
        # ws streams: stream_id -> asyncio.Queue[dict ASGI-events for receive]
        ws_streams: dict[str, asyncio.Queue] = {}

        async def send(frame: tp.AnyFrame) -> None:
            async with send_lock:
                await ws.send(tp.encode(frame))

        async def handle_http_req(req: tp.HttpReq) -> None:
            try:
                res = await self._dispatch_http(req)
            except Exception as e:  # noqa: BLE001
                log.exception("dispatch http_req failed: %s", e)
                res = tp.HttpRes(
                    stream_id=req.stream_id, status=500,
                    body_b64=base64.b64encode(
                        f"app dispatch error: {e}".encode()
                    ).decode("ascii"),
                )
            await send(res)

        async def handle_ws_open(open_frame: tp.WsOpen) -> None:
            q: asyncio.Queue = asyncio.Queue()
            ws_streams[open_frame.stream_id] = q
            # 进 ASGI dispatch — 出错也要发 WsClose 给 hub
            try:
                await self._dispatch_ws(open_frame, q, send)
            except Exception as e:  # noqa: BLE001
                log.exception("dispatch ws_open failed: %s", e)
                try:
                    await send(tp.WsClose(
                        stream_id=open_frame.stream_id,
                        code=1011, reason=f"app_error:{e!s}"[:120],
                    ))
                except Exception:
                    pass
            finally:
                ws_streams.pop(open_frame.stream_id, None)

        async for raw in ws:
            try:
                frame = tp.decode(raw)
            except Exception:
                log.warning("bad frame: %r", raw[:200] if isinstance(raw, str) else raw)
                continue
            if isinstance(frame, tp.Ping):
                await send(tp.Pong(stream_id=frame.stream_id))
            elif isinstance(frame, tp.HttpReq):
                asyncio.create_task(handle_http_req(frame))
            elif isinstance(frame, tp.WsOpen):
                asyncio.create_task(handle_ws_open(frame))
            elif isinstance(frame, (tp.WsMsg, tp.WsClose)):
                q = ws_streams.get(frame.stream_id)
                if q is None:
                    log.debug("orphan ws frame stream=%s", frame.stream_id)
                    continue
                if isinstance(frame, tp.WsMsg):
                    payload = base64.b64decode(frame.payload_b64) if frame.payload_b64 else b""
                    if frame.is_binary:
                        q.put_nowait({
                            "type": "websocket.receive", "bytes": payload,
                        })
                    else:
                        q.put_nowait({
                            "type": "websocket.receive",
                            "text": payload.decode("utf-8", errors="replace"),
                        })
                else:
                    q.put_nowait({
                        "type": "websocket.disconnect", "code": frame.code,
                    })
            elif isinstance(frame, tp.Control):
                log.debug("control op=%s (server -> app) ignored", frame.op)
            else:
                log.debug("frame %s unhandled",
                          getattr(frame, "type", "?"))

    # ---------- ASGI in-process dispatch ----------

    async def _dispatch_http(self, req: tp.HttpReq) -> tp.HttpRes:
        """构造 ASGI HTTP scope, 调本进程 FastAPI app, 收 response."""
        body = base64.b64decode(req.body_b64) if req.body_b64 else b""

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": req.method,
            "scheme": "http",
            "path": req.path,
            "raw_path": req.path.encode("utf-8"),
            "query_string": req.query.encode("utf-8"),
            "root_path": "",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1"))
                for k, v in req.headers.items()
            ],
            "client": ("hub", 0),
            "server": ("app", 0),
            # via_hub 标记 — app 端 auth 看到 → 跳过 bearer 验证, 信 Hub auth.
            # 外部直连进来的 HTTP request 由 ASGI server 创建 scope, 不存在
            # 这个 key, 因此不可被伪造.
            "state": {"via_hub": True, "hub_user_id": req.user_id or ""},
        }

        body_sent = False

        async def receive() -> dict:
            nonlocal body_sent
            if body_sent:
                return {"type": "http.disconnect"}
            body_sent = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        status: int = 500
        headers: list[tuple[bytes, bytes]] = []
        body_chunks: list[bytes] = []

        async def send(message: dict) -> None:
            nonlocal status
            t = message.get("type")
            if t == "http.response.start":
                status = int(message.get("status", 500))
                headers.extend(message.get("headers") or [])
            elif t == "http.response.body":
                b = message.get("body") or b""
                if b:
                    body_chunks.append(b)
            # http.response.trailers 暂不处理

        await self.asgi_app(scope, receive, send)

        out_headers: dict[str, str] = {}
        for hk, hv in headers:
            try:
                out_headers[hk.decode("latin-1")] = hv.decode("latin-1")
            except Exception:
                pass

        merged = b"".join(body_chunks)
        return tp.HttpRes(
            stream_id=req.stream_id,
            status=status,
            headers=out_headers,
            body_b64=base64.b64encode(merged).decode("ascii"),
        )


    # ---------- ASGI in-process WS dispatch ----------

    async def _dispatch_ws(
        self, open_frame: tp.WsOpen, in_queue: asyncio.Queue,
        send_frame,
    ) -> None:
        """构造 ASGI websocket scope, 跑本进程 ws handler.

        - receive 从 in_queue 拿事件 (websocket.connect 由本函数 push 第一条;
          后续 websocket.receive / websocket.disconnect 由外层 _loop 推).
        - send 把 ASGI send 事件转成 tp.WsMsg / tp.WsClose 帧发回 hub.
        """
        scope_path = open_frame.path
        # 提取 sid 给 path_params (ASGI 不强制要, 但 FastAPI 路由会从 raw_path 自己 match)
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": scope_path,
            "raw_path": scope_path.encode("utf-8"),
            "query_string": open_frame.query.encode("utf-8"),
            "root_path": "",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1"))
                for k, v in open_frame.headers.items()
            ],
            "subprotocols": [],
            "client": ("hub", 0),
            "server": ("app", 0),
            "state": {
                "via_hub": True,
                "hub_user_id": open_frame.user_id or "",
            },
        }

        # 初始 websocket.connect
        await in_queue.put({"type": "websocket.connect"})

        async def receive() -> dict:
            return await in_queue.get()

        async def send(message: dict) -> None:
            t = message.get("type")
            if t == "websocket.accept":
                # nothing — hub 已对 user 端 accept, app 端不需要回传
                return
            if t == "websocket.send":
                if message.get("bytes") is not None:
                    payload = message["bytes"]
                    is_binary = True
                else:
                    payload = (message.get("text") or "").encode("utf-8")
                    is_binary = False
                await send_frame(tp.WsMsg(
                    stream_id=open_frame.stream_id,
                    payload_b64=base64.b64encode(payload).decode("ascii"),
                    is_binary=is_binary,
                ))
            elif t == "websocket.close":
                code = int(message.get("code") or 1000)
                reason = str(message.get("reason") or "")
                await send_frame(tp.WsClose(
                    stream_id=open_frame.stream_id,
                    code=code, reason=reason,
                ))

        try:
            await self.asgi_app(scope, receive, send)
        finally:
            # ASGI 端如果没主动 close, 我们也补一个让 hub 关 user_ws
            try:
                await send_frame(tp.WsClose(
                    stream_id=open_frame.stream_id, code=1000,
                ))
            except Exception:
                pass


    # ---------- Metadata pump (M-Hub-2) ----------

    async def _metadata_pump(self, ws) -> None:
        """订阅 session_manager 的全局事件流, 翻成 control 帧推 hub.

        manager.global_subscribe() 先 yield 一个 {type:snapshot} (启动时所有
        sessions), 之后实时推 session_state / session_deleted 等.
        """
        from .session_manager import manager
        send_lock = asyncio.Lock()

        async def send_control(op: str, data: dict) -> None:
            frame = tp.Control(stream_id="*", op=op, data=data)
            async with send_lock:
                await ws.send(tp.encode(frame))

        try:
            async for evt in manager.global_subscribe():
                etype = evt.get("type")
                if etype == "snapshot":
                    await send_control(
                        "sessions_snapshot",
                        {"sessions": evt.get("sessions") or []},
                    )
                elif etype == "session_state":
                    # status_payload 已平铺在 evt 顶层 (除了 type)
                    payload = {k: v for k, v in evt.items() if k != "type"}
                    await send_control("session_state", payload)
                elif etype == "session_deleted":
                    await send_control(
                        "session_removed", {"id": evt.get("id")},
                    )
                else:
                    log.debug("metadata pump: skip evt type=%s", etype)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("metadata pump crashed")


def maybe_start(asgi_app) -> HubClient | None:
    """读 env, 决定是否启动 HubClient. 没配 hub_url 时返回 None."""
    hub_url = os.environ.get("CCR_HUB_URL", "").strip()
    token = os.environ.get("CCR_HUB_DEVICE_TOKEN", "").strip()
    if not hub_url or not token:
        log.info("hub_client not started (CCR_HUB_URL or token empty)")
        return None
    name = os.environ.get("CCR_HUB_APP_NAME") or socket.gethostname()
    client = HubClient(asgi_app, hub_url=hub_url, device_token=token,
                       app_name=name)
    client.start()
    log.info("hub_client launched url=%s name=%s", hub_url, name)
    return client
