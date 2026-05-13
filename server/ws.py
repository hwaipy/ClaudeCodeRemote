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
from .permission_gateway import gateway
from .session_manager import manager

log = logging.getLogger(__name__)
router = APIRouter()


async def _handle_permission_decision(sess_id: str, msg: dict) -> None:
    req_id = msg.get("req_id")
    decision = (msg.get("decision") or "").strip()
    persist = msg.get("persist")  # None / "tool" / "command"
    reason = msg.get("reason") or ("user " + decision)
    if decision not in ("allow", "deny") or not req_id:
        log.warning("bad permission_decision payload: %r", msg)
        return
    payload = {"behavior": decision, "message": reason}
    if decision == "allow":
        # 工具白名单要 input 透传，让 claude 真正执行（updatedInput 可省略）
        pass
    ok = await gateway.resolve(req_id, payload, persist=persist)
    if not ok:
        log.warning("permission_decision for unknown req: %s (sess=%s)",
                    req_id, sess_id)


@router.websocket("/ws-global")
async def ws_global(ws: WebSocket) -> None:
    """全局活动流：前端主页订阅，server 推送所有 sess 的状态变化。"""
    token = ws.query_params.get("token")
    if not check_ws_token(token):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
        return
    await ws.accept()
    try:
        async for msg in manager.global_subscribe():
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws-global push crashed")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


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
                    raw = msg.get("content")
                    # 支持 str 或 [{type:text|image, ...}, ...]
                    if isinstance(raw, str):
                        content = raw.strip()
                        if not content:
                            continue
                    elif isinstance(raw, list):
                        content = [b for b in raw
                                   if isinstance(b, dict) and b.get("type")]
                        if not content:
                            continue
                    else:
                        continue

                    def _proc_alive(s):
                        return (s.proc is not None and s.proc.proc is not None
                                and s.proc.proc.returncode is None)

                    if not _proc_alive(sess):
                        log.info("sess %s proc dead; auto-resume", sess.id)
                        resumed = await manager.resume(sess.id)
                        if resumed is None or not _proc_alive(resumed):
                            log.error("auto-resume failed for %s", sess.id)
                            continue
                    log.debug("ws->claude: user_message sess=%s type=%s",
                              session_id, type(content).__name__)
                    # 先注入 user_input 事件（DB 持久化 + 前端 echo）
                    await manager.inject_event(sess, {
                        "type": "user_input", "content": content,
                    })
                    # 写 stdin。如果 proc 是中断刚死的，drain 会抛 BrokenPipe /
                    # ConnectionResetError；这种情况 resume 起新 proc 再 retry 一次
                    try:
                        await sess.proc.send_user_message(content)
                    except (ConnectionResetError, BrokenPipeError, RuntimeError) as e:
                        log.warning("send_user_message failed (%s); resume+retry",
                                    type(e).__name__)
                        resumed = await manager.resume(sess.id)
                        if resumed is None or not _proc_alive(resumed):
                            log.error("re-resume failed for %s", sess.id)
                            continue
                        try:
                            await sess.proc.send_user_message(content)
                        except Exception:
                            log.exception("retry send_user_message still failed for %s",
                                          sess.id)
                elif kind == "permission_decision":
                    await _handle_permission_decision(sess.id, msg)
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
