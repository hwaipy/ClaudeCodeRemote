"""WebSocket：双向 JSON。

客户端 → 服务端：
    {"type": "user_message", "content": "..."}

服务端 → 客户端：
    {"seq": N, "ts": float, "event": <stream-json 原样>}
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from .auth import check_ws_token
from .session_manager import manager

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/{session_id}")
async def ws_session(ws: WebSocket, session_id: str) -> None:
    token = ws.query_params.get("token")
    if not check_ws_token(token):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
        return
    sess = await manager.get(session_id)
    if not sess:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="session not found")
        return

    await ws.accept()

    async def push_events() -> None:
        log.debug("push_events task started for %s", session_id)
        try:
            async for env in manager.subscribe(sess):
                await ws.send_json(env)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("push_events crashed")

    async def recv_messages() -> None:
        log.debug("recv_messages task started for %s", session_id)
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "user_message":
                    content = (msg.get("content") or "").strip()
                    if content:
                        log.debug("ws->claude: user_message %d chars sess=%s",
                                  len(content), session_id)
                        await sess.proc.send_user_message(content)
                elif kind == "ping":
                    await ws.send_json({"type": "pong", "ts": msg.get("ts")})
                else:
                    log.warning("unknown ws message type: %r", kind)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("recv_messages crashed")

    pusher = asyncio.create_task(push_events())
    receiver = asyncio.create_task(recv_messages())
    try:
        # 任意一个结束就收尾
        done, pending = await asyncio.wait(
            [pusher, receiver], return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        try:
            await ws.close()
        except Exception:
            pass
