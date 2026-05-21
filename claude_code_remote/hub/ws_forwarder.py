"""WS forwarder — 透传 user 的 WebSocket 给反向 WS 上的 app.

ccr-hub-spec.html §3 §5:
  user 连 wss://hub/ws/<sid>  →  hub 接管 + 找对应 app
                              →  发 WsOpen 帧 (path, user_id) 给 app
                              →  双向桥接 WsMsg / WsClose 帧

M-Hub-1 简化: app 路由跟 HTTP forwarder 同样 — 取该 user 第一个 online app.
M-Hub-2 起按 sid 查 sessions_cache 决定路由.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..shared import tunnel_proto as tp
from . import auth as hub_auth, db as hub_db
from .tunnel import _WsStream, registry

log = logging.getLogger("ccr.hub.ws_forwarder")

router = APIRouter()


async def _safe_close(ws: WebSocket, code: int, reason: str = "") -> None:
    if ws.client_state == WebSocketState.DISCONNECTED:
        return
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


async def _forward_ws(user_ws: WebSocket, path: str) -> None:
    """通用 ws forward: hub accept user_ws → 找 app → 双向 multiplex."""
    # auth: 走 cookie 优先, ?token= 备选 (跟 spec 一致, SPA 大部分用 cookie)
    user_id = await hub_auth.get_user_id(user_ws.cookies.get("ccr_sess"))
    qs_token = user_ws.query_params.get("token")
    if not user_id and qs_token:
        # 备选: ?token=<session-cookie-value> (SW / 跨 origin 场景)
        user_id = await hub_auth.get_user_id(qs_token)
    if not user_id:
        await user_ws.close(code=1008, reason="unauthorized")
        return

    # 按 sid 路由 — 查 sessions_cache 找归属 app, 不能挑"第一个 online".
    # 多 app 场景下挑错 → forward 到不存在该 sid 的 app → close → SPA 重连
    # 死循环. /ws-global 没有 sid, 退化到第一个 online (仅做实时 delta source).
    online = None
    parts = path.split("/")
    is_session_ws = (len(parts) >= 3 and parts[1] == "ws" and parts[2])
    if is_session_ws:
        sid = parts[2]
        rows = await hub_db.list_user_sessions(user_id)
        target_app_id = None
        for r in rows:
            if r["sid"] == sid:
                target_app_id = r["app_id"]
                break
        if target_app_id:
            for o in registry.online_apps():
                if o.user_id == user_id and o.app_id == target_app_id:
                    online = o
                    break
        # cache 没该 sid 或对应 app offline — 拒, 不 fallback
    else:
        # /ws-global / 其它无 sid 路径: 第一个 online 兜底
        for o in registry.online_apps():
            if o.user_id == user_id:
                online = o
                break
    if online is None:
        await user_ws.close(code=1011, reason="app_offline_or_no_session")
        return

    await user_ws.accept()
    stream_id = "w-" + secrets.token_hex(8)
    stream = _WsStream(stream_id=stream_id, user_ws=user_ws)
    online.register_ws_stream(stream)

    # 发 WsOpen 给 app — 它会构造 ASGI ws scope, 自动 accept.
    headers = {}
    for k, v in user_ws.headers.items():
        if k.lower() in {"host", "connection", "upgrade",
                          "sec-websocket-key", "sec-websocket-version",
                          "sec-websocket-extensions", "cookie"}:
            continue
        headers[k] = v
    headers["x-ccr-user-id"] = user_id
    open_frame = tp.WsOpen(
        stream_id=stream_id,
        path=path,
        user_id=user_id,
        query=str(user_ws.url.query or ""),
        headers=headers,
    )
    try:
        await online.send_frame(open_frame)
    except Exception:
        log.exception("send WsOpen failed")
        online.remove_ws_stream(stream_id)
        await _safe_close(user_ws, code=1011, reason="app_send_failed")
        return

    # 双向 task
    async def user_to_app() -> None:
        try:
            while True:
                msg = await user_ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    break
                if "bytes" in msg and msg["bytes"] is not None:
                    payload = msg["bytes"]
                    is_binary = True
                else:
                    payload = (msg.get("text") or "").encode("utf-8")
                    is_binary = False
                frame = tp.WsMsg(
                    stream_id=stream_id,
                    payload_b64=base64.b64encode(payload).decode("ascii"),
                    is_binary=is_binary,
                )
                try:
                    await online.send_frame(frame)
                except Exception:
                    log.warning("send WsMsg to app failed")
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("user_to_app crashed")
        finally:
            # 通知 app
            if not stream.closed:
                try:
                    await online.send_frame(tp.WsClose(stream_id=stream_id))
                except Exception:
                    pass

    async def app_to_user() -> None:
        try:
            while True:
                frame = await stream.inbound.get()
                if isinstance(frame, tp.WsClose):
                    break
                payload = base64.b64decode(frame.payload_b64) if frame.payload_b64 else b""
                if frame.is_binary:
                    await user_ws.send_bytes(payload)
                else:
                    await user_ws.send_text(payload.decode("utf-8", errors="replace"))
        except Exception:
            log.exception("app_to_user crashed")

    t1 = asyncio.create_task(user_to_app(), name="ws_user_to_app")
    t2 = asyncio.create_task(app_to_user(), name="ws_app_to_user")
    try:
        done, pending = await asyncio.wait(
            [t1, t2], return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        online.remove_ws_stream(stream_id)
        await _safe_close(user_ws, code=1000)


@router.websocket("/ws/{sid}")
async def ws_session(ws: WebSocket, sid: str):
    await _forward_ws(ws, f"/ws/{sid}")


@router.websocket("/ws-global")
async def ws_global(ws: WebSocket):
    await _forward_ws(ws, "/ws-global")
