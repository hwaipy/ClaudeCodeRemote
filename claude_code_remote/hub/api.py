"""Hub HTTP API — hub-only endpoints.

- POST /api/hub/login          : email+password → 设 ccr_sess cookie
- POST /api/hub/logout         : 清 cookie
- GET  /api/hub/apps           : 列 user 的 apps + 在线状态
- DELETE /api/hub/apps/<id>    : revoke 一个 app, 踢掉它的 ws
- POST /api/hub/pair           : 登录后生成短期 pairing code
- POST /api/hub/pair/redeem    : (无 cookie) 用 code 换 device_token
"""
from __future__ import annotations

import secrets

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


@router.delete("/apps/{app_id}")
async def delete_app(app_id: str, ccr_sess: str | None = Cookie(None)):
    user_id = await _require_user(ccr_sess)
    # 确认 app 归属
    rows = await hub_db.list_apps_for_user(user_id)
    if not any(r["id"] == app_id for r in rows):
        raise HTTPException(status_code=404, detail="app_not_found")
    ok = await hub_db.revoke_app(app_id)
    if not ok:
        # 已 revoked
        pass
    # 踢掉它的反向 WS
    await registry.disconnect_app(app_id)
    return {"ok": True}


# ---- pairing flow ----

class RedeemReq(BaseModel):
    code: str
    app_name: str


@router.post("/pair")
async def create_pair(ccr_sess: str | None = Cookie(None)):
    user_id = await _require_user(ccr_sess)
    return await hub_db.create_pairing(user_id, ttl_seconds=300)


@router.post("/pair/redeem")
async def redeem_pair(body: RedeemReq):
    """无 cookie. App CLI 拿 code 换 device_token + app_id + user_id."""
    user_id = await hub_db.consume_pairing(body.code.strip())
    if not user_id:
        raise HTTPException(status_code=403, detail="bad_or_expired_code")
    name = (body.app_name or "").strip() or "untitled-app"
    device_token = "tok-" + secrets.token_hex(16)
    app_id = await hub_db.create_app(user_id, name, device_token)
    return {
        "device_token": device_token,
        "app_id": app_id,
        "user_id": user_id,
        "app_name": name,
    }
