"""Hub HTTP API — hub-only endpoints.

- POST /api/hub/login   : email+password → 设 ccr_sess cookie
- POST /api/hub/logout  : 清 cookie
- GET  /api/hub/apps    : 列 user 的 apps + 在线状态
"""
from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel

from . import auth, db as hub_db
from .tunnel import registry

router = APIRouter(prefix="/api/hub")

_COOKIE_NAME = "ccr_sess"


class LoginReq(BaseModel):
    email: str
    password: str


async def _require_user(session_id: str | None) -> str:
    user_id = auth.get_user_id(session_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user_id


@router.post("/login")
async def login(body: LoginReq, response: Response):
    uid = await hub_db.verify_login(body.email, body.password)
    if not uid:
        raise HTTPException(status_code=401, detail="bad_credentials")
    sid = auth.create_session(uid)
    response.set_cookie(
        _COOKIE_NAME, sid,
        httponly=True, samesite="lax", path="/", max_age=30 * 24 * 3600,
    )
    return {"ok": True, "user_id": uid}


@router.post("/logout")
async def logout(response: Response, ccr_sess: str | None = Cookie(None)):
    if ccr_sess:
        auth.destroy_session(ccr_sess)
    response.delete_cookie(_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/apps")
async def list_apps(ccr_sess: str | None = Cookie(None)):
    user_id = await _require_user(ccr_sess)
    rows = await hub_db.list_apps_for_user(user_id)
    online_set = set(registry.online_app_ids())
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "last_seen_at": r["last_seen_at"],
            "created_at": r["created_at"],
            "online": r["id"] in online_set,
        }
        for r in rows
    ]
