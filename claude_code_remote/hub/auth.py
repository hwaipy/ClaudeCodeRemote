"""Hub user auth — 简陋的 session cookie (v0 单 admin, 没必要 JWT).

session_id 随机, 内存 dict 映射到 user_id. 进程重启 cookie 失效, 重新登录即可.
后续多 user / 持久化用 sqlite session store.
"""
from __future__ import annotations

import secrets
import time

# session_id -> {user_id, created_at}
_sessions: dict[str, dict] = {}


def create_session(user_id: str) -> str:
    sid = secrets.token_urlsafe(24)
    _sessions[sid] = {"user_id": user_id, "created_at": time.time()}
    return sid


def get_user_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    row = _sessions.get(session_id)
    return row["user_id"] if row else None


def destroy_session(session_id: str) -> None:
    _sessions.pop(session_id, None)
