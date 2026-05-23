"""Hub 第三方 OAuth2 登录 — Google / GitHub / Gitee (国内替代阿里云).

挂在 /api/hub/auth/<provider>/{start,callback}. 启用前提:env 里给该 provider
设了 CLIENT_ID + CLIENT_SECRET, 否则该 provider 整段 disabled (前端不显示按钮).

Flow:
  1. user 点登录按钮 → GET /api/hub/auth/<provider>/start
     → server 生成 state (随机, 5min TTL cache), redirect 到 provider authorize_url
  2. provider 回调 GET /api/hub/auth/<provider>/callback?code=...&state=...
     → server 验 state → POST token_url 换 access_token → GET userinfo 拿
       sub + email + display → upsert oauth_links → ensure user → 设
       ccr_sess cookie → 302 回 hub home
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Callable
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from . import auth, db as hub_db

log = logging.getLogger("ccr.hub.oauth")
router = APIRouter(prefix="/api/hub/auth")

# 短期 state 缓存 (CSRF 防御 + flow 关联). value = {provider, exp}
_STATES: dict[str, dict] = {}
_STATE_TTL = 600


def _save_state(state: str, provider: str) -> None:
    # 顺手 purge 过期
    now = time.time()
    for k in list(_STATES.keys()):
        if _STATES[k]["exp"] < now:
            del _STATES[k]
    _STATES[state] = {"provider": provider, "exp": now + _STATE_TTL}


def _consume_state(state: str, provider: str) -> bool:
    e = _STATES.pop(state, None)
    if not e or e["exp"] < time.time():
        return False
    return e["provider"] == provider


# ---------- Provider registry ----------

class _Provider:
    """一个 OAuth2 provider 的配置 + userinfo 解析."""
    def __init__(self, key: str, label: str, color: str,
                 client_id_env: str, client_secret_env: str,
                 authorize_url: str, token_url: str, userinfo_url: str,
                 scope: str,
                 parse_user: Callable[[dict], tuple[str, str | None, str | None]],
                 userinfo_headers: dict | None = None) -> None:
        self.key = key
        self.label = label
        self.color = color
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.scope = scope
        self.parse_user = parse_user
        self.userinfo_headers = userinfo_headers or {}
        self.client_id = os.environ.get(client_id_env, "").strip()
        self.client_secret = os.environ.get(client_secret_env, "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)


def _parse_google(j: dict) -> tuple[str, str | None, str | None]:
    return str(j["sub"]), j.get("email"), j.get("name") or j.get("email")


def _parse_github(j: dict) -> tuple[str, str | None, str | None]:
    # /user 返 id (int) + email (有时是 null, 需要再请求 /user/emails); 简化:
    # 直接取 public email. 没就用 login + @users.noreply.github.com 兜底.
    sub = str(j["id"])
    email = j.get("email") or f"{j['login']}@users.noreply.github.com"
    display = j.get("name") or j.get("login")
    return sub, email, display


def _parse_gitee(j: dict) -> tuple[str, str | None, str | None]:
    sub = str(j["id"])
    email = j.get("email") or f"{j['login']}@gitee.users.noreply"
    display = j.get("name") or j.get("login")
    return sub, email, display


PROVIDERS: dict[str, _Provider] = {
    "google": _Provider(
        key="google", label="Google", color="#4285F4",
        client_id_env="CCR_HUB_OAUTH_GOOGLE_CLIENT_ID",
        client_secret_env="CCR_HUB_OAUTH_GOOGLE_CLIENT_SECRET",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scope="openid email profile",
        parse_user=_parse_google,
    ),
    "github": _Provider(
        key="github", label="GitHub", color="#181717",
        client_id_env="CCR_HUB_OAUTH_GITHUB_CLIENT_ID",
        client_secret_env="CCR_HUB_OAUTH_GITHUB_CLIENT_SECRET",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        scope="read:user user:email",
        parse_user=_parse_github,
        userinfo_headers={"Accept": "application/vnd.github+json"},
    ),
    "gitee": _Provider(
        key="gitee", label="Gitee", color="#C71D23",
        client_id_env="CCR_HUB_OAUTH_GITEE_CLIENT_ID",
        client_secret_env="CCR_HUB_OAUTH_GITEE_CLIENT_SECRET",
        authorize_url="https://gitee.com/oauth/authorize",
        token_url="https://gitee.com/oauth/token",
        userinfo_url="https://gitee.com/api/v5/user",
        scope="user_info emails",
        parse_user=_parse_gitee,
    ),
}


def enabled_providers() -> list[dict]:
    return [
        {"key": p.key, "label": p.label, "color": p.color}
        for p in PROVIDERS.values() if p.enabled
    ]


def _hub_origin() -> str:
    """获取自身的对外 URL — env CCR_HUB_ORIGIN 优先 (passkey 已有). fallback
    用 https://<rp_id>."""
    o = os.environ.get("CCR_HUB_ORIGIN", "").strip()
    if o:
        return o.rstrip("/")
    rp = os.environ.get("CCR_HUB_RP_ID", "localhost").strip()
    return f"https://{rp}"


def _callback_url(provider: str) -> str:
    return f"{_hub_origin()}/api/hub/auth/{provider}/callback"


# ---------- Endpoints ----------

@router.get("/{provider}/start")
async def oauth_start(provider: str):
    p = PROVIDERS.get(provider)
    if not p or not p.enabled:
        raise HTTPException(status_code=404, detail="provider_disabled")
    state = secrets.token_urlsafe(24)
    _save_state(state, provider)
    qs = urlencode({
        "client_id": p.client_id,
        "redirect_uri": _callback_url(provider),
        "response_type": "code",
        "scope": p.scope,
        "state": state,
        "access_type": "offline",   # Google 用; 其它 provider 忽略
    })
    return Response(
        status_code=302,
        headers={"Location": f"{p.authorize_url}?{qs}"},
    )


@router.get("/{provider}/callback")
async def oauth_callback(provider: str, request: Request):
    p = PROVIDERS.get(provider)
    if not p or not p.enabled:
        raise HTTPException(status_code=404, detail="provider_disabled")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing_code_or_state")
    if not _consume_state(state, provider):
        raise HTTPException(status_code=400, detail="bad_state")

    # 换 access_token
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(
                p.token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _callback_url(provider),
                    "client_id": p.client_id,
                    "client_secret": p.client_secret,
                },
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            tok = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] token exchange failed: %s", provider, e)
            raise HTTPException(status_code=502, detail=f"token_exchange_failed: {e}")
        access_token = tok.get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="no_access_token")

        # 拉 userinfo
        try:
            hdrs = {
                "Authorization": f"Bearer {access_token}",
                **p.userinfo_headers,
            }
            ur = await client.get(p.userinfo_url, headers=hdrs)
            ur.raise_for_status()
            user_json = ur.json()
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] userinfo failed: %s", provider, e)
            raise HTTPException(status_code=502, detail=f"userinfo_failed: {e}")

    try:
        sub, email, display = p.parse_user(user_json)
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] parse_user failed: %s; payload=%r",
                    provider, e, user_json)
        raise HTTPException(status_code=502, detail="parse_user_failed")

    # 1. 已存在的 link → 直接拿 user_id
    link = await hub_db.find_oauth_link(provider, sub)
    if link:
        user_id = link["user_id"]
    else:
        # 2. 没 link, 但 email 匹配现有 user → 链接到该 user
        user_id = await hub_db.find_user_by_email(email) if email else None
        # 3. 都没 → 新建 user (无密码)
        if not user_id:
            if not email:
                email = f"{provider}-{sub}@oauth.local"
            user_id = await hub_db.create_oauth_user(email)
        await hub_db.upsert_oauth_link(provider, sub, user_id, email, display)

    # 刷 last_used_at + display 万一变
    await hub_db.upsert_oauth_link(provider, sub, user_id, email, display)

    sid = await auth.create_session(user_id)
    resp = Response(status_code=302, headers={"Location": "/"})
    resp.set_cookie(
        "ccr_sess", sid,
        httponly=True, samesite="lax", path="/",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    log.info("[%s] login ok user_id=%s sub=%s email=%s",
             provider, user_id, sub, email)
    return resp
