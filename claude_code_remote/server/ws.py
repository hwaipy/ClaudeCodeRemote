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


async def _handle_askuser_answer(sess, msg: dict) -> None:
    """前端把 AskUserQuestion 的回答收集完 → resolve gateway 里挂起的 hook 调用。
    真正给 claude stdin 灬 tool_result 的逻辑在 api._handle_askuser_hook 里完成
    （它在 return allow 之前先写 stdin，避免 CLI 的 auto-fail）。"""
    tool_use_id = (msg.get("tool_use_id") or "").strip()
    answers = msg.get("answers")
    if not tool_use_id or not isinstance(answers, list):
        log.warning("bad askuser_answer payload: %r", msg)
        return
    ok = await gateway.resolve_askuser_by_tool_id(tool_use_id, answers)
    if not ok:
        log.warning("askuser_answer: no pending hook req for tool_use_id=%s (stale?)",
                    tool_use_id)


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


def _via_hub(ws: WebSocket) -> bool:
    return bool(ws.scope.get("state", {}).get("via_hub"))


@router.websocket("/ws-global")
async def ws_global(ws: WebSocket) -> None:
    """全局活动流：前端主页订阅，server 推送所有 sess 的状态变化.

    长空闲时 hub 中间的 nginx (proxy_read_timeout 900s) 会 idle close ws,
    部分代理 silent-close (浏览器看不到 close 事件, ws 假活), 后续 session
    activity 推过来全部丢失 — 表现就是 "home 卡片不刷新". 加 30s 一次
    keepalive frame, 让链路永远有 traffic, nginx 永不 idle close;
    任何中间环节真断, send 会 fail, 浏览器立即收到 close 触发 reconnect.
    """
    import asyncio as _asyncio
    if not _via_hub(ws):
        token = ws.query_params.get("token")
        if not check_ws_token(token):
            await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
            return
    await ws.accept()

    async def push_events() -> None:
        async for msg in manager.global_subscribe():
            await ws.send_json(msg)

    async def keepalive() -> None:
        while True:
            await _asyncio.sleep(30)
            await ws.send_json({"type": "_keepalive"})

    pusher = _asyncio.create_task(push_events())
    pinger = _asyncio.create_task(keepalive())
    try:
        done, pending = await _asyncio.wait(
            [pusher, pinger], return_when=_asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
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
    import time as _time
    _t0 = _time.perf_counter()
    def _ms(): return int((_time.perf_counter() - _t0) * 1000)
    log.info("DBG ws-handler-enter %s sess=%s", _ms(), session_id)
    if not _via_hub(ws):
        token = ws.query_params.get("token")
        if not check_ws_token(token):
            await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
            return
    sess = await manager.get(session_id)
    if not sess:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="session not found")
        return
    _alive = (sess.proc is not None and sess.proc.proc is not None
              and sess.proc.proc.returncode is None)
    log.info("DBG ws-proc-alive-pre-accept %s alive=%s", _ms(), _alive)
    await ws.accept()
    log.info("DBG ws-accepted %s", _ms())

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
                    # First user message after deactivate/stash → reactivate
                    if sess.deactivated_at is not None or sess.stashed_at is not None:
                        await manager.activate(sess.id)
                    # client_msg_id (optional): SPA outbox 用作 ack 配对.
                    # 透传到 user_input event 里, client 收回时 match → 标 sent.
                    client_msg_id = msg.get("client_msg_id")
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
                    # 先注入 user_input 事件（DB 持久化 + 前端 echo）.
                    # client_msg_id 也带在 event 里, SPA 收到时 outbox ack.
                    _evt = {"type": "user_input", "content": content}
                    if client_msg_id:
                        _evt["client_msg_id"] = client_msg_id
                    await manager.inject_event(sess, _evt)
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
                elif kind == "askuser_answer":
                    await _handle_askuser_answer(sess, msg)
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
