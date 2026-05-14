"""PermissionGateway：PreToolUse hook 桥接器调进来 → 推 WS 等用户决定 → 回桥接器。

内存白名单（M3 范围；M4 持久化到 SQLite）：
    {ccr_session_id: {"tools": set[str], "commands": set[str]}}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


def cmd_fingerprint(tool_name: str, tool_input: dict[str, Any]) -> str:
    """精确匹配指纹：tool_name + 关键参数。Bash 用 command；文件类用 file_path。"""
    if tool_name == "Bash":
        key = tool_input.get("command", "")
    elif tool_name in ("Read", "Write", "Edit"):
        key = tool_input.get("file_path", "")
    else:
        try:
            key = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
        except Exception:
            key = repr(tool_input)
    h = hashlib.sha256(f"{tool_name}\x00{key}".encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{h}"


@dataclass
class PermissionRequest:
    req_id: str
    ccr_session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    claude_payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


@dataclass
class AskUserRequest:
    """AskUserQuestion 工具的挂起请求：hook 进来后我们把整条 hook 调用挂着，
    等用户在前端回答完再 resolve（future 拿到答案 list），然后给 stdin 灌 tool_result
    并返回 hook allow。这样 CLI 不会触发 "Answer questions?" auto-fail。"""
    req_id: str
    ccr_session_id: str
    tool_use_id: str
    tool_input: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


VALID_MODES = ("manual", "accept_edits", "plan", "allow_all")
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


class PermissionGateway:
    def __init__(self) -> None:
        self._pending: dict[str, PermissionRequest] = {}
        self._askuser_pending: dict[str, AskUserRequest] = {}  # req_id -> AskUserRequest
        # ccr_session_id -> {"tools": set[str], "commands": set[str (fingerprint)]}
        self._allow: dict[str, dict[str, set[str]]] = {}
        # ccr_session_id -> "manual" | "allow_all"  (per-session permission mode)
        self._modes: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _allow_state(self, sid: str) -> dict[str, set[str]]:
        if sid not in self._allow:
            self._allow[sid] = {"tools": set(), "commands": set()}
        return self._allow[sid]

    def get_mode(self, sid: str) -> str:
        return self._modes.get(sid, "manual")

    async def set_mode(self, sid: str, mode: str) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {mode}")
        self._modes[sid] = mode
        try:
            from . import db
            await db.save_permission_mode(sid, mode)
        except Exception:
            log.exception("save_permission_mode failed for sess=%s", sid)

    def pending_for_session(self, sid: str) -> list[PermissionRequest]:
        return [r for r in self._pending.values() if r.ccr_session_id == sid]

    def is_preapproved(self, sid: str, tool_name: str, tool_input: dict[str, Any]) -> bool:
        # AskUserQuestion 不走 preapproved—它由独立的 askuser 挂起流程处理（见 open_askuser）
        if tool_name == "AskUserQuestion":
            return False
        mode = self._modes.get(sid, "manual")
        if mode == "allow_all":
            return True
        if mode == "accept_edits" and tool_name in _EDIT_TOOLS:
            return True
        # plan mode never preapproves; handled in api.permission_wait (auto-deny)
        st = self._allow.get(sid)
        if not st:
            return False
        if tool_name in st["tools"]:
            return True
        if cmd_fingerprint(tool_name, tool_input) in st["commands"]:
            return True
        return False

    def is_plan_mode(self, sid: str) -> bool:
        return self._modes.get(sid) == "plan"

    # ---------- AskUserQuestion 挂起 ----------
    async def open_askuser(self, sess_id: str, tool_use_id: str,
                           tool_input: dict[str, Any]) -> AskUserRequest:
        req = AskUserRequest(
            req_id="aq-" + uuid.uuid4().hex[:10],
            ccr_session_id=sess_id,
            tool_use_id=tool_use_id,
            tool_input=tool_input,
        )
        async with self._lock:
            self._askuser_pending[req.req_id] = req
        return req

    async def resolve_askuser_by_tool_id(self, tool_use_id: str, answer: Any) -> bool:
        """前端 askuser_answer 收到 → 按 tool_use_id 找挂起请求并把答案塞给 future。"""
        async with self._lock:
            target_rid = None
            for rid, req in self._askuser_pending.items():
                if req.tool_use_id == tool_use_id:
                    target_rid = rid
                    break
            if target_rid is None:
                return False
            req = self._askuser_pending.pop(target_rid)
        if not req.future.done():
            req.future.set_result(answer)
        return True

    def get_pending_askuser(self, sid: str) -> list[AskUserRequest]:
        return [r for r in self._askuser_pending.values() if r.ccr_session_id == sid]

    async def open_request(self, *, ccr_session_id: str, claude_payload: dict[str, Any]) -> PermissionRequest:
        tool_name = claude_payload.get("tool_name", "")
        tool_input = claude_payload.get("tool_input") or {}
        tool_use_id = claude_payload.get("tool_use_id", "")
        req = PermissionRequest(
            req_id="prm-" + uuid.uuid4().hex[:10],
            ccr_session_id=ccr_session_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            claude_payload=claude_payload,
        )
        async with self._lock:
            self._pending[req.req_id] = req
        return req

    async def resolve(self, req_id: str, decision: dict[str, Any],
                      persist: str | None = None) -> bool:
        """前端 / 自动批准把决定 set 给 future。persist ∈ {None, 'tool', 'command'}。"""
        async with self._lock:
            req = self._pending.pop(req_id, None)
        if req is None:
            return False
        if persist in ("tool", "command") and decision.get("behavior") == "allow":
            self.remember(req.ccr_session_id, persist, req.tool_name, req.tool_input)
            key = (req.tool_name if persist == "tool"
                   else cmd_fingerprint(req.tool_name, req.tool_input))
            try:
                from . import db
                await db.save_perm(req.ccr_session_id, persist, key)
            except Exception:
                log.exception("save_perm failed for sess=%s", req.ccr_session_id)
        if not req.future.done():
            req.future.set_result(decision)
        return True

    async def load_for_session(self, sid: str) -> None:
        """server 启动时按需把 DB 里该 session 的白名单 + 权限模式加载进内存。"""
        from . import db
        try:
            rows = await db.load_perms(sid)
        except Exception:
            log.exception("load_perms failed for sess=%s", sid)
            rows = []
        st = self._allow_state(sid)
        for scope, key in rows:
            if scope == "tool":
                st["tools"].add(key)
            elif scope == "command":
                st["commands"].add(key)
        try:
            mode = await db.load_permission_mode(sid)
        except Exception:
            log.exception("load_permission_mode failed for sess=%s", sid)
            mode = "manual"
        if mode in VALID_MODES and mode != "manual":
            self._modes[sid] = mode

    def remember(self, sid: str, scope: str, tool_name: str, tool_input: dict[str, Any]) -> None:
        st = self._allow_state(sid)
        if scope == "tool":
            st["tools"].add(tool_name)
        elif scope == "command":
            st["commands"].add(cmd_fingerprint(tool_name, tool_input))

    def get_pending(self, sid: str) -> list[PermissionRequest]:
        return [r for r in self._pending.values() if r.ccr_session_id == sid]

    async def cancel_for_session(self, sid: str) -> None:
        async with self._lock:
            to_drop = [rid for rid, r in self._pending.items() if r.ccr_session_id == sid]
            for rid in to_drop:
                req = self._pending.pop(rid)
                if not req.future.done():
                    req.future.set_result({"behavior": "deny", "message": "session ended"})
            aq_drop = [rid for rid, r in self._askuser_pending.items() if r.ccr_session_id == sid]
            for rid in aq_drop:
                aq = self._askuser_pending.pop(rid)
                if not aq.future.done():
                    aq.future.set_result(None)   # None = 取消（session 结束）


gateway = PermissionGateway()
