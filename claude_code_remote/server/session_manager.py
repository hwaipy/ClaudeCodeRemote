"""SessionManager：进程注册表 + 事件广播 + SQLite 持久化 + hibernate 调度。

每个 active session 一个独立 task 把 ClaudeProcess.events() 抽干，关键事件落 DB +
fan-out 给所有当前 WS 订阅者。订阅者首次进来先从 DB 重放历史，再接实时流。

Idle > HIBERNATE_IDLE_S 自动 SIGTERM 子进程；--resume 时拿 claude_session_id 拉起。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from . import db
from .claude_process import ClaudeProcess

log = logging.getLogger(__name__)

HIBERNATE_IDLE_S = float(os.environ.get("CCR_HIBERNATE_IDLE_S", "1800"))  # 30 min
HIBERNATE_TICK_S = float(os.environ.get("CCR_HIBERNATE_TICK_S", "60"))

# 哪些事件落 DB（其它视为流式噪音，可从落了 DB 的事件重建 UI）
_PERSIST_TOP = {"assistant", "user", "result", "user_input"}


def _classify(evt: dict[str, Any]) -> str | None:
    """决定一个事件的持久化 kind，None 表示不存。"""
    t = evt.get("type")
    if t in _PERSIST_TOP:
        return t
    if t == "system" and evt.get("subtype") == "init":
        return "system_init"
    if t == "_ccr":
        sub = evt.get("subtype")
        if sub == "permission_request":
            return "perm_req"
        if sub == "permission_resolved":
            return "perm_resolved"
        if sub == "askuser_request":
            return "askuser_req"
        if sub == "askuser_resolved":
            return "askuser_resolved"
    return None


@dataclass
class Session:
    id: str
    cwd: str
    name: str
    created_at: float
    proc: ClaudeProcess | None
    claude_session_id: str | None = None
    last_activity_at: float = field(default_factory=time.time)
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    pump_task: asyncio.Task[None] | None = None
    seq: int = 0
    finished: bool = False
    hibernated: bool = False
    # User-facing "inactive" flag. Set via deactivate(); cleared when user
    # sends a fresh message. Cards in this state render in the Inactive
    # section on home.
    deactivated_at: float | None = None
    pending_permissions: int = 0          # 当前等待用户决定的权限请求数
    needs_action_detail: str | None = None  # post_turn_summary 报告的待办（None=无）
    # 一轮对话激活中：用户发出 → result 之间（含工具调用），用作"工作中"判定
    active_turn: bool = False
    # 工具实时状态缓存：tool_use_id -> {name, partial_input, input, result, is_error, completed}
    # 用于前端展开"进行中"的工具卡时按需轮询拉当前累积状态（包括流到一半的 input_json_delta）
    live_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 当前 message 内 content_block index → tool_use_id 的映射（message_start 时清空）
    block_to_tool: dict[int, str] = field(default_factory=dict)
    # 当前/上一轮对话的运行时状态（后端维护、前端只读，前端刷新/重连不丢）
    turn_started_at: float | None = None   # 最近一次 user_input 的 envelope ts
    turn_ended_at:   float | None = None   # 最近一次 result 的 envelope ts；下一轮 user_input 时清
    turn_output_tokens: int = 0             # 当前轮已累积的 output tokens
    turn_prior_output:  int = 0             # 当前 message 之前的累积量（跨 message 用）
    cur_model:        str = ""
    cur_input_tokens: int = 0               # 最近 message 的 fresh input_tokens
    cur_input_total:  int = 0               # input_tokens + cache_read + cache_creation（用于 ctx%）

    def envelope(self, evt: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        return {"seq": self.seq, "ts": time.time(), "event": evt}

    def compute_state(self) -> str:
        if self.pending_permissions > 0:
            return "waiting_permission"
        if self.needs_action_detail:
            return "needs_input"
        if self.proc is not None and self.proc.proc and self.proc.proc.returncode is None:
            return "busy" if self.active_turn else "idle"
        if self.hibernated:
            return "hibernated"
        if self.finished:
            return "finished"
        return "idle"

    def status_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cwd": self.cwd,
            "claude_session_id": self.claude_session_id,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "state": self.compute_state(),
            "pending_permissions": self.pending_permissions,
            "needs_action_detail": self.needs_action_detail,
            "is_inactive": self.deactivated_at is not None,
        }


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._hibernate_task: asyncio.Task[None] | None = None
        # 全局订阅者：监听所有会话状态变化（前端主页用）
        self._global_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    # ---------- 全局事件广播 ----------

    def _broadcast_status(self, sess: Session) -> None:
        payload = {"type": "session_state", **sess.status_payload()}
        for q in list(self._global_subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                log.warning("global subscriber queue full")

    async def global_subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """前端订阅全局活动流：先发一份当前所有 sess 的状态快照，再走实时。"""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self._global_subscribers.add(q)
        try:
            async with self._lock:
                snapshot = [s.status_payload() for s in self.sessions.values()]
            yield {"type": "snapshot", "sessions": snapshot}
            while True:
                yield await q.get()
        finally:
            self._global_subscribers.discard(q)

    # ---------- 启动 / 关闭 ----------

    async def startup(self) -> None:
        """server 启动时调：DB 已 init 之后，把 DB 里未删除的会话装载成 hibernated 状态。"""
        from .permission_gateway import gateway
        rows = await db.list_sessions()
        for r in rows:
            sess = Session(
                id=r["id"],
                cwd=r["cwd"],
                name=r["name"],
                created_at=r["created_at"],
                proc=None,
                claude_session_id=r["claude_session_id"],
                last_activity_at=r["last_activity_at"],
                hibernated=True,
                finished=r["finished_at"] is not None,
                deactivated_at=r.get("deactivated_at"),
            )
            sess.seq = await db.max_seq(r["id"])
            async with self._lock:
                self.sessions[sess.id] = sess
            await gateway.load_for_session(sess.id)
            # 清理孤儿 perm_req（server 上次重启时 hook 桥接器留下，gateway 已丢）
            orphans = await db.find_orphan_perm_reqs(sess.id)
            for rid in orphans:
                sess.seq += 1
                stale_evt = {
                    "type": "_ccr", "subtype": "permission_resolved",
                    "req_id": rid, "decision": "stale",
                    "message": "会话恢复时此请求已失效（server 重启）",
                }
                await db.append_message(sess.id, sess.seq, time.time(),
                                         "perm_resolved", stale_evt)
            if orphans:
                log.info("marked %d orphan perm_req(s) as stale for %s",
                         len(orphans), sess.id)
        self._hibernate_task = asyncio.create_task(
            self._hibernate_loop(), name="hibernate-scheduler",
        )
        log.info("session manager loaded %d sessions from DB", len(rows))

    async def shutdown(self) -> None:
        if self._hibernate_task:
            self._hibernate_task.cancel()
        async with self._lock:
            items = list(self.sessions.values())
        for s in items:
            if s.proc is not None:
                await s.proc.terminate()
        for s in items:
            if s.pump_task:
                s.pump_task.cancel()

    # ---------- spawn / resume / delete ----------

    async def spawn(self, cwd: str, name: str = "") -> Session:
        local_id = "ccr-" + uuid.uuid4().hex[:12]
        proc = ClaudeProcess(cwd=cwd, ccr_session_id=local_id)
        await proc.start()
        sess = Session(
            id=local_id, cwd=cwd, name=name or "untitled",
            created_at=time.time(), proc=proc,
        )
        await db.insert_session(local_id, sess.name, cwd, sess.created_at)
        async with self._lock:
            self.sessions[local_id] = sess
        sess.pump_task = asyncio.create_task(self._pump(sess), name=f"pump-{local_id}")
        self._broadcast_status(sess)
        return sess

    async def resume(self, sess_id: str) -> Session | None:
        async with self._lock:
            sess = self.sessions.get(sess_id)
        if not sess:
            return None
        if sess.proc is not None and sess.proc.proc and sess.proc.proc.returncode is None:
            # 已经活着
            return sess
        if not sess.claude_session_id:
            log.warning("resume %s: no claude_session_id; spawning fresh in same cwd", sess_id)
        proc = ClaudeProcess(cwd=sess.cwd, ccr_session_id=sess.id,
                             resume_session_id=sess.claude_session_id)
        await proc.start()
        sess.proc = proc
        sess.hibernated = False
        sess.finished = False
        sess.last_activity_at = time.time()
        # 旧 proc 被 interrupt / 崩溃时 message_start..message_stop 可能未配对，
        # busy / needs_action 残留旧值；新 proc 干净起步，重置一下
        sess.busy = False
        sess.needs_action_detail = None
        await db.mark_resumed(sess_id)
        sess.pump_task = asyncio.create_task(self._pump(sess), name=f"pump-{sess_id}")
        log.info("resumed %s (claude=%s)", sess_id, sess.claude_session_id)
        self._broadcast_status(sess)
        return sess

    async def interrupt(self, sess_id: str) -> bool:
        """打断当前会话：杀掉子进程让其立即返回，session 进入 hibernated；下一次 send / resume 会重启。"""
        async with self._lock:
            sess = self.sessions.get(sess_id)
        if not sess or sess.proc is None:
            return False
        try:
            await sess.proc.terminate()
        except Exception:
            log.exception("terminate during interrupt failed for %s", sess_id)
        return True

    async def rename(self, sess_id: str, name: str) -> bool:
        """Update the user-facing name of a session."""
        async with self._lock:
            sess = self.sessions.get(sess_id)
        if not sess:
            return False
        sess.name = name
        await db.update_name(sess_id, name)
        self._broadcast_status(sess)
        return True

    async def deactivate(self, sess_id: str) -> bool:
        """Mark a session inactive. Frontend renders it under "Inactive"."""
        async with self._lock:
            sess = self.sessions.get(sess_id)
        if not sess:
            return False
        sess.deactivated_at = time.time()
        await db.mark_deactivated(sess_id)
        self._broadcast_status(sess)
        return True

    async def activate(self, sess_id: str) -> bool:
        """Clear the inactive flag (called when user sends a new message)."""
        async with self._lock:
            sess = self.sessions.get(sess_id)
        if not sess or sess.deactivated_at is None:
            return False
        sess.deactivated_at = None
        await db.mark_activated(sess_id)
        self._broadcast_status(sess)
        return True

    async def delete(self, sess_id: str) -> bool:
        async with self._lock:
            sess = self.sessions.pop(sess_id, None)
        if not sess:
            return False
        if sess.proc is not None:
            try:
                await sess.proc.terminate()
            except Exception:
                log.exception("terminate during delete failed for %s", sess_id)
        if sess.pump_task:
            sess.pump_task.cancel()
        await db.mark_deleted(sess_id)
        # 广播一个 deletion 通知（前端从列表中移除）
        for q in list(self._global_subscribers):
            try:
                q.put_nowait({"type": "session_deleted", "id": sess_id})
            except asyncio.QueueFull:
                pass
        return True

    # ---------- pump ----------

    async def _pump(self, sess: Session) -> None:
        log.debug("pump start for %s", sess.id)
        assert sess.proc is not None
        try:
            async for evt in sess.proc.events():
                log.debug("pump got: type=%s subtype=%s", evt.get("type"),
                          evt.get("subtype") or (evt.get("event") or {}).get("type"))
                # 抓 claude 真实 session_id
                if (evt.get("type") == "system" and evt.get("subtype") == "init"
                        and sess.claude_session_id is None):
                    real = evt.get("session_id")
                    if real:
                        sess.claude_session_id = real
                        await db.update_claude_sid(sess.id, real)
                        log.info("claude session_id assigned: %s (sess=%s)", real, sess.id)
                await self._deliver(sess, evt)
        except Exception:
            log.exception("pump crashed for session %s", sess.id)
        finally:
            sess.finished = sess.proc is None or (sess.proc.proc is not None
                                                  and sess.proc.proc.returncode is not None)
            terminator = sess.envelope({"type": "_internal", "subtype": "pump_done"})
            for q in list(sess.subscribers):
                try:
                    q.put_nowait(terminator)
                except asyncio.QueueFull:
                    pass

    def _is_askuser_autofail(self, sess: Session, evt: dict[str, Any]) -> bool:
        """匹配 claude --print 模式下 AskUserQuestion 的 CLI 兜底 fail 输出。
        条件：user.tool_result，tool_use_id 是已知的 AskUserQuestion，is_error=true，
              content == "Answer questions?"（CLI 硬编码字串）。"""
        if evt.get("type") != "user":
            return False
        msg = evt.get("message") or {}
        blocks = msg.get("content") or []
        if not isinstance(blocks, list) or not blocks:
            return False
        # 这种自动 fail 通常单独一条 block；保险起见只要其中一条命中就吞整条
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            if not b.get("is_error"):
                continue
            content = b.get("content")
            if not isinstance(content, str) or content != "Answer questions?":
                continue
            tid = b.get("tool_use_id")
            if not tid:
                continue
            tool = sess.live_tools.get(tid) if hasattr(sess, "live_tools") else None
            if tool and tool.get("name") == "AskUserQuestion":
                return True
        return False

    async def _deliver(self, sess: Session, evt: dict[str, Any]) -> None:
        from .tool_lazy import strip_live_event, update_live_tools
        # 先把 raw 事件灌进 live_tools，给 /tool/{id} 查最新累积状态用
        try:
            update_live_tools(sess, evt)
        except Exception:
            log.exception("update_live_tools failed for %s", sess.id)
        # 拦截 AskUserQuestion 的"自动 fail"伪 tool_result：claude --print 模式下任意
        # AskUserQuestion 调用结束后 CLI 会立刻 emit 一条 is_error=true content="Answer questions?"
        # 我们的真答案要等用户点击才走 stdin 灌进去；如果不拦住这条伪结果，前端会先看到它
        # 然后把卡片打成"已回答"。CLI 自己看到我们后续注入的真 tool_result 会用最后那个
        # 继续推进，所以前端这边吞掉伪 fail 不会影响 claude 的对话流。
        if self._is_askuser_autofail(sess, evt):
            log.debug("dropped AskUserQuestion autofail tool_result for sess=%s", sess.id)
            return
        env = sess.envelope(evt)
        kind = _classify(evt)
        if kind is not None:
            try:
                # DB 始终入全量原文；前端按需走 /tool/{id} 拉回
                await db.append_message(sess.id, env["seq"], env["ts"], kind, evt)
                await db.update_activity(sess.id, env["ts"])
            except Exception:
                log.exception("persist message failed for %s", sess.id)
        sess.last_activity_at = env["ts"]
        # 状态变化检测（必须在 envelope/落库之后；并在 fan-out 之前确定，
        # 以便 broadcast_status 时拿到最新值）
        state_dirty = self._apply_state_signals(sess, evt)
        # 维护 turn state（轮次起止时间、output tokens、当前 model / input total）
        # 后端是这些值的唯一源，前端刷新/重连不会丢
        self._update_turn_state(sess, evt, env["ts"])
        # 起止时刻立刻推一份给所有 WS 客户端，前端不再自己算 turnStartAt/turnEndAt
        _t = evt.get("type")
        if _t == "user_input":
            # 新一轮开始：只更新 turnStartAt（不发 token，避免可见的"骤降为 0"）
            self._broadcast_turn_state(sess, include_tokens=False)
        elif _t == "result":
            self._broadcast_turn_state(sess, include_tokens=True)
        # 广播前瘦身：工具调用的 input / result 用占位代替，input_json_delta 整段丢
        stripped = strip_live_event(evt, sess)
        if stripped is not None:
            broadcast_env = env if stripped is evt else {**env, "event": stripped}
            for q in list(sess.subscribers):
                try:
                    q.put_nowait(broadcast_env)
                except asyncio.QueueFull:
                    log.warning("subscriber queue full, dropping event for %s", sess.id)
        if state_dirty:
            self._broadcast_status(sess)

    def _broadcast_turn_state(self, sess: Session, *, include_tokens: bool = True) -> None:
        """推 turn_state 给所有订阅者。`include_tokens=False` 时不带 token 字段——用于
        user_input 时机：让前端的计时器更新，但 token 显示保持上一轮终值，等 message_start 再刷新。"""
        evt = {
            "type": "_ccr",
            "subtype": "turn_state",
            "turn_started_at": sess.turn_started_at,
            "turn_ended_at":   sess.turn_ended_at,
            "model":           sess.cur_model,
        }
        if include_tokens:
            evt["output_tokens"] = sess.turn_output_tokens
            evt["input_tokens"]  = sess.cur_input_tokens
            evt["input_total"]   = sess.cur_input_total
        env = sess.envelope(evt)
        for q in list(sess.subscribers):
            try:
                q.put_nowait(env)
            except asyncio.QueueFull:
                pass

    def _update_turn_state(self, sess: Session, evt: dict[str, Any], env_ts: float) -> None:
        """维护后端轮次状态：起止时间 / output tokens / 当前 model & input_total。
        逻辑对齐前端 handleUserInput / handleResult / handleStreamEvent。"""
        t = evt.get("type")
        if t == "user_input":
            sess.turn_started_at = env_ts
            sess.turn_ended_at = None
            sess.turn_output_tokens = 0
            sess.turn_prior_output = 0
            return
        if t == "result":
            sess.turn_ended_at = env_ts
            u = evt.get("usage") or {}
            ot = u.get("output_tokens")
            if isinstance(ot, int):
                sess.turn_output_tokens = max(sess.turn_output_tokens, ot)
            return
        if t == "stream_event":
            ev = evt.get("event") or {}
            et = ev.get("type")
            if et == "message_start":
                msg = ev.get("message") or {}
                m = msg.get("model")
                if isinstance(m, str) and m:
                    sess.cur_model = m
                u = msg.get("usage") or {}
                inp = u.get("input_tokens")
                if isinstance(inp, int):
                    sess.cur_input_tokens = inp
                    sess.cur_input_total = (inp
                                          + (u.get("cache_read_input_tokens") or 0)
                                          + (u.get("cache_creation_input_tokens") or 0))
                # 跨 message 累加：把上一条 message 的产出滚成 prior，下一条用 prior + 当条 output 算 cur
                sess.turn_prior_output = sess.turn_output_tokens
                ot = u.get("output_tokens")
                if isinstance(ot, int):
                    sess.turn_output_tokens = sess.turn_prior_output + ot
            elif et == "message_delta":
                u = ev.get("usage") or {}
                ot = u.get("output_tokens")
                if isinstance(ot, int):
                    sess.turn_output_tokens = sess.turn_prior_output + ot
                inp = u.get("input_tokens")
                if isinstance(inp, int):
                    sess.cur_input_tokens = inp
                    sess.cur_input_total = (inp
                                          + (u.get("cache_read_input_tokens") or 0)
                                          + (u.get("cache_creation_input_tokens") or 0))
            elif et == "message_stop":
                sess.turn_prior_output = sess.turn_output_tokens

    def _apply_state_signals(self, sess: Session, evt: dict[str, Any]) -> bool:
        """根据事件更新 sess 状态字段；返回是否有变化（影响 compute_state）。"""
        t = evt.get("type")
        sub = evt.get("subtype")
        before = sess.compute_state()
        before_pending = sess.pending_permissions
        before_need = sess.needs_action_detail

        if t == "_ccr" and sub == "permission_request":
            sess.pending_permissions += 1
        elif t == "_ccr" and sub == "permission_resolved":
            sess.pending_permissions = max(0, sess.pending_permissions - 1)
        elif t == "system" and sub == "post_turn_summary":
            detail = (evt.get("needs_action") or "").strip()
            sess.needs_action_detail = detail or None
        elif t == "user_input":
            # 新一轮开始：从用户发出消息到 result 之间都算"工作中"（覆盖 tool 调用整段）
            sess.active_turn = True
            sess.needs_action_detail = None
        elif t == "result":
            # 一轮真正结束（claude --print 收尾事件）
            sess.active_turn = False

        after = sess.compute_state()
        return (after != before
                or sess.pending_permissions != before_pending
                or sess.needs_action_detail != before_need)

    async def inject_event(self, sess: Session, event: dict[str, Any]) -> None:
        await self._deliver(sess, event)

    # ---------- subscribe ----------

    async def subscribe(self, sess: Session) -> AsyncIterator[dict[str, Any]]:
        """初次推送：仅"最近一次问答 OR 最近 20 条"取多者；剩余历史走 HTTP 按需拉。
        然后发 backlog_done（带 first_seq + has_more），再走实时队列。"""
        from .permission_gateway import gateway
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        sess.subscribers.add(q)
        try:
            INITIAL_MIN = 200
            RECENT_TAIL = 30            # 没 user_input 兜底时的"最近"批大小
            last_user_seq = await db.find_last_user_seq(sess.id)
            # 第一批 = 最近一轮（user_input → ...）；没有则退化为 tail
            if last_user_seq is not None:
                recent = await db.load_messages_from(sess.id, last_user_seq)
            else:
                recent = await db.load_messages_tail(sess.id, RECENT_TAIL)
            # 第二批 = recent 之前的，总数控制在 INITIAL_MIN
            remaining = max(0, INITIAL_MIN - len(recent))
            if recent and remaining > 0:
                earlier = await db.load_messages_before(sess.id, recent[0][0], remaining)
            else:
                earlier = []
            history_count = len(earlier) + len(recent)
            first_seq = (earlier[0][0] if earlier
                         else (recent[0][0] if recent else None))
            has_more = (await db.count_messages_before(sess.id, first_seq) > 0
                        if first_seq is not None else False)

            from .tool_lazy import strip_payload_for_backlog
            # 第一批：最新的一轮（让前端尽快揭幕）
            for seq, ts, kind, payload in recent:
                yield {"seq": seq, "ts": ts, "event": strip_payload_for_backlog(payload)}
            turn_state = None
            if sess.turn_started_at is not None:
                turn_state = {
                    "turn_started_at": sess.turn_started_at,
                    "turn_ended_at":   sess.turn_ended_at,
                    "output_tokens":   sess.turn_output_tokens,
                    "model":           sess.cur_model,
                    "input_tokens":    sess.cur_input_tokens,
                    "input_total":     sess.cur_input_total,
                }
            # first_paint：前端收到这条就揭幕（recent 已渲染好），后续 earlier 渲到屏外 buffer
            yield {
                "seq": -1, "ts": time.time(),
                "event": {"type": "_ccr", "subtype": "first_paint",
                          "turn_state": turn_state},
            }
            # 第二批：更早的历史（按 seq 升序），前端会缓冲到 fragment 一次性 prepend
            for seq, ts, kind, payload in earlier:
                yield {"seq": seq, "ts": ts, "event": strip_payload_for_backlog(payload)}
            yield {
                "seq": -1, "ts": time.time(),
                "event": {"type": "_ccr", "subtype": "backlog_done",
                          "history_count": history_count,
                          "first_seq": first_seq,
                          "has_more": has_more,
                          "turn_state": turn_state},
            }
            # 补：当前 gateway 里仍 pending 的请求重发一遍（前端兜底渲染）
            for req in gateway.get_pending(sess.id):
                yield {
                    "seq": -1, "ts": time.time(),
                    "event": {
                        "type": "_ccr", "subtype": "permission_request",
                        "req_id": req.req_id,
                        "tool_name": req.tool_name,
                        "tool_input": req.tool_input,
                        "tool_use_id": req.tool_use_id,
                        "replay": True,
                    },
                }
            # AskUserQuestion 的挂起请求也重发，让前端在重连时能继续看到交互卡
            for aq in gateway.get_pending_askuser(sess.id):
                yield {
                    "seq": -1, "ts": time.time(),
                    "event": {
                        "type": "_ccr", "subtype": "askuser_request",
                        "req_id": aq.req_id,
                        "tool_use_id": aq.tool_use_id,
                        "tool_input": aq.tool_input,
                        "replay": True,
                    },
                }
            while True:
                env = await q.get()
                yield env
                if env.get("event", {}).get("subtype") == "pump_done":
                    return
        finally:
            sess.subscribers.discard(q)

    # ---------- list / get ----------

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self._lock:
            items = list(self.sessions.values())
        out = [s.status_payload() for s in items]
        out.sort(key=lambda r: -r["created_at"])
        return out

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            return self.sessions.get(session_id)

    # ---------- hibernate ----------

    async def _hibernate_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HIBERNATE_TICK_S)
                await self._sweep_idle()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("hibernate loop crashed")

    async def _sweep_idle(self) -> None:
        now = time.time()
        async with self._lock:
            items = list(self.sessions.values())
        for sess in items:
            if sess.proc is None:
                continue
            if sess.proc.proc is None or sess.proc.proc.returncode is not None:
                # 进程已退；标 hibernated 但 mem 保留以便 resume
                sess.hibernated = True
                continue
            if now - sess.last_activity_at < HIBERNATE_IDLE_S:
                continue
            log.info("hibernating %s (idle=%ss)", sess.id,
                     int(now - sess.last_activity_at))
            try:
                await sess.proc.terminate()
            except Exception:
                log.exception("terminate during hibernate failed for %s", sess.id)
            await db.mark_hibernated(sess.id)
            sess.hibernated = True
            sess.proc = None
            self._broadcast_status(sess)


manager = SessionManager()
