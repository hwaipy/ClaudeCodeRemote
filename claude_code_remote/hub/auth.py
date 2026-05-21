"""Hub user auth — session 持久化到 db (重启 hub 后 cookie 不失效).

session id (cookie value) = token_urlsafe(24), 入 db auth_sessions 表 +
TTL. 老的内存 dict 已废弃.
"""
from __future__ import annotations

from . import db as hub_db

SESSION_TTL_SECONDS = 30 * 24 * 3600


async def create_session(user_id: str) -> str:
    return await hub_db.create_auth_session(user_id, SESSION_TTL_SECONDS)


async def get_user_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    return await hub_db.get_auth_session_user(session_id)


async def destroy_session(session_id: str) -> None:
    if session_id:
        await hub_db.destroy_auth_session(session_id)
