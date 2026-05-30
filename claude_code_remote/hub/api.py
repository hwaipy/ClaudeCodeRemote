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
    user_id = await auth.get_user_id(session_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user_id


@router.post("/login")
async def login(body: LoginReq, response: Response):
    uid = await hub_db.verify_login(body.email, body.password)
    if not uid:
        raise HTTPException(status_code=401, detail="bad_credentials")
    sid = await auth.create_session(uid)
    response.set_cookie(
        _COOKIE_NAME, sid,
        httponly=True, samesite="lax", path="/", max_age=auth.SESSION_TTL_SECONDS,
    )
    return {"ok": True, "user_id": uid}


@router.post("/logout")
async def logout(response: Response, ccr_sess: str | None = Cookie(None)):
    if ccr_sess:
        await auth.destroy_session(ccr_sess)
    response.delete_cookie(_COOKIE_NAME, path="/")
    return {"ok": True}


async def me_handler(ccr_sess: str | None) -> dict:
    """SPA probe — 让前端知道当前是 hub 模式 + 登录身份 + apps list.
    路径 /api/me (不在 /api/hub/ 下), 让 hub + local 用同一 endpoint."""
    # OAuth providers (env 配置过 client_id/secret 的) — 即使未登录也返,
    # 这样 login 页能渲对应按钮.
    from .oauth import enabled_providers
    oauth = enabled_providers()
    user_id = await auth.get_user_id(ccr_sess)
    if not user_id:
        return {"mode": "hub", "user_id": None, "apps": [],
                "oauth_providers": oauth}
    apps_rows = await hub_db.list_apps_for_user(user_id)
    online_set = set(registry.online_app_ids())
    apps = [
        {
            "id": r["id"],
            "name": r["name"],
            "online": r["id"] in online_set,
            "created_at": r["created_at"],
            "total_online_seconds": int(r.get("total_online_seconds") or 0),
        }
        for r in apps_rows
    ]
    return {"mode": "hub", "user_id": user_id, "apps": apps,
            "oauth_providers": oauth}


@router.get("/apps")
async def list_apps(ccr_sess: str | None = Cookie(None)):
    user_id = await _require_user(ccr_sess)
    rows = await hub_db.list_apps_for_user(user_id)
    online_by_id = {a.app_id: a for a in registry.online_apps()}
    out = []
    for r in rows:
        online_app = online_by_id.get(r["id"])
        out.append({
            "id": r["id"],
            "name": r["name"],
            "last_seen_at": r["last_seen_at"],
            "created_at": r["created_at"],
            "online": online_app is not None,
            # 持久化累计 + 当前 session 的运行时长 — 前端只看 total 即可.
            "total_online_seconds": int(r.get("total_online_seconds") or 0),
            "connected_at": online_app.connected_at if online_app else None,
            "short_host": r.get("short_host"),   # /files/<sh>/<fid> 用
        })
    return out


class ReorderAppsReq(BaseModel):
    ordered_ids: list[str]


@router.put("/apps/reorder")
async def reorder_apps(
    body: ReorderAppsReq, ccr_sess: str | None = Cookie(None),
):
    """按 body.ordered_ids 数组顺序重写当前 user 的 apps.sort_order.

    body.ordered_ids 必须是 user 当前所有 app_id 的 permutation. 多机同步
    走 hub db 持久化 — 任何设备 GET /api/hub/apps 都按新顺序返回.
    """
    user_id = await _require_user(ccr_sess)
    rows = await hub_db.list_apps_for_user(user_id)
    owned = {r["id"] for r in rows}
    target = list(dict.fromkeys(body.ordered_ids))   # dedupe, keep order
    if not (set(target) <= owned):
        raise HTTPException(400, detail="ordered_ids contains non-owned apps")
    n = await hub_db.reorder_apps_for_user(user_id, target)
    return {"updated": n}


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
