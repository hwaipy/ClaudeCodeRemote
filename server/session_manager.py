"""SessionManager：进程注册表 + 事件广播。M1 内存版。M4 上 SQLite。

每个 session 一个独立 task 把 ClaudeProcess.events() 抽干，事件 fan-out 给所有
当前 WS 订阅者。事件也会留一份完整 backlog 在内存里，新订阅者进来先收 backlog
再接实时流（页面刷新时不丢上下文）。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .claude_process import ClaudeProcess

log = logging.getLogger(__name__)


@dataclass
class Session:
    id: str            # 本地稳定 id（spawn 时生成，永不变）
    cwd: str
    name: str
    created_at: float
    proc: ClaudeProcess
    claude_session_id: str | None = None  # 从 system/init 拿到
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    pump_task: asyncio.Task[None] | None = None
    seq: int = 0
    finished: bool = False

    def envelope(self, evt: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        return {"seq": self.seq, "ts": time.time(), "event": evt}


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def spawn(self, cwd: str, name: str = "") -> Session:
        # 本地稳定 id（独立于 claude 自己的 session_id）。
        local_id = "ccr-" + uuid.uuid4().hex[:12]
        proc = ClaudeProcess(cwd=cwd, ccr_session_id=local_id)
        await proc.start()
        sess = Session(
            id=local_id,
            cwd=cwd,
            name=name or "untitled",
            created_at=time.time(),
            proc=proc,
        )
        async with self._lock:
            self.sessions[local_id] = sess
        sess.pump_task = asyncio.create_task(self._pump(sess), name=f"pump-{local_id}")
        return sess

    async def _pump(self, sess: Session) -> None:
        log.debug("pump start for %s", sess.id)
        try:
            async for evt in sess.proc.events():
                log.debug("pump got: type=%s subtype=%s", evt.get("type"),
                          evt.get("subtype") or (evt.get("event") or {}).get("type"))
                # 抓 claude 真实 session_id 存到 sess（不再切换 sess.id）
                if (evt.get("type") == "system" and evt.get("subtype") == "init"
                        and sess.claude_session_id is None):
                    real = evt.get("session_id")
                    if real:
                        sess.claude_session_id = real
                        log.info("claude session_id assigned: %s (sess=%s cwd=%s)",
                                 real, sess.id, sess.cwd)
                env = sess.envelope(evt)
                sess.events.append(env)
                # fan-out（拷一份 list 避免迭代时 set 变化）
                for q in list(sess.subscribers):
                    try:
                        q.put_nowait(env)
                    except asyncio.QueueFull:
                        log.warning("subscriber queue full, dropping event for %s", sess.id)
        except Exception:
            log.exception("pump crashed for session %s", sess.id)
        finally:
            sess.finished = True
            # 给订阅者发一个收尾信号
            terminator = sess.envelope({"type": "_internal", "subtype": "pump_done"})
            sess.events.append(terminator)
            for q in list(sess.subscribers):
                try:
                    q.put_nowait(terminator)
                except asyncio.QueueFull:
                    pass

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self._lock:
            items = list(self.sessions.values())
        out = []
        for s in items:
            out.append({
                "id": s.id,
                "claude_session_id": s.claude_session_id,
                "name": s.name,
                "cwd": s.cwd,
                "created_at": s.created_at,
                "finished": s.finished,
                "event_count": len(s.events),
            })
        out.sort(key=lambda r: -r["created_at"])
        return out

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            return self.sessions.get(session_id)

    async def subscribe(self, sess: Session) -> AsyncIterator[dict[str, Any]]:
        """订阅一个会话：先重放 backlog，再走实时队列。"""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        # snapshot backlog before adding to subscribers，防止重复
        backlog = list(sess.events)
        sess.subscribers.add(q)
        try:
            for env in backlog:
                yield env
            while True:
                env = await q.get()
                yield env
                if env.get("event", {}).get("subtype") == "pump_done":
                    return
        finally:
            sess.subscribers.discard(q)

    async def inject_event(self, sess: Session, event: dict[str, Any]) -> None:
        """把一个自造事件注入会话流（走 envelope + fan-out + backlog）。
        用于权限请求、桥接消息等不来自 claude stream 的事件。"""
        env = sess.envelope(event)
        sess.events.append(env)
        for q in list(sess.subscribers):
            try:
                q.put_nowait(env)
            except asyncio.QueueFull:
                log.warning("subscriber queue full, dropping injected event for %s", sess.id)

    async def shutdown(self) -> None:
        async with self._lock:
            items = list(self.sessions.values())
        for s in items:
            await s.proc.terminate()
        for s in items:
            if s.pump_task:
                s.pump_task.cancel()


manager = SessionManager()
