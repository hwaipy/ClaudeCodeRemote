"""Hub 第三方 OAuth2 登录 — Google / GitHub / Gitee / 飞书 / 钉钉.

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
    """一个 OAuth2 provider 的配置 + userinfo 解析.

    钉钉不走标准 OAuth2 字段:
      - token endpoint 要 JSON body 而不是 form (token_json=True)
      - 字段名是 camelCase (clientId/clientSecret/grantType), 通过
        build_token_body 自定义
      - userinfo 用 x-acs-dingtalk-access-token header 而不是 Bearer
        (userinfo_auth='x_acs_dingtalk')
    """
    def __init__(self, key: str, label: str, color: str,
                 client_id_env: str, client_secret_env: str,
                 authorize_url: str, token_url: str, userinfo_url: str,
                 scope: str,
                 parse_user: Callable[[dict], tuple[str, str | None, str | None]],
                 userinfo_headers: dict | None = None,
                 token_json: bool = False,
                 userinfo_auth: str = "bearer",
                 build_token_body: Callable[[str, str, str, str], dict] | None = None,
                 fetch_user_info: Callable | None = None) -> None:
        self.key = key
        self.label = label
        self.color = color
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.userinfo_url = userinfo_url
        self.scope = scope
        self.parse_user = parse_user
        self.userinfo_headers = userinfo_headers or {}
        self.token_json = token_json
        self.userinfo_auth = userinfo_auth
        self.build_token_body = build_token_body or _standard_token_body
        # QQ 这种"先 GET /me 拿 openid 再 GET /user_info"的多步 user info
        # 流程没法用单一 userinfo_url 表达, 让 provider 自定义整个 fetch.
        # 签名: async (httpx.AsyncClient, access_token: str, provider) -> dict
        self.fetch_user_info = fetch_user_info
        self.client_id = os.environ.get(client_id_env, "").strip()
        self.client_secret = os.environ.get(client_secret_env, "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)


def _standard_token_body(code: str, redirect_uri: str,
                         client_id: str, client_secret: str) -> dict:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }


def _dingtalk_token_body(code: str, redirect_uri: str,
                         client_id: str, client_secret: str) -> dict:
    # 钉钉 token endpoint 字段名是 camelCase; redirect_uri 不需要带.
    return {
        "clientId": client_id,
        "clientSecret": client_secret,
        "code": code,
        "grantType": "authorization_code",
    }


def _qq_token_body(code: str, redirect_uri: str,
                   client_id: str, client_secret: str) -> dict:
    # QQ 互联 token endpoint 默认返 form-urlencoded; 加 fmt=json 才返 JSON.
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "fmt": "json",
    }


async def _fetch_qq_user_info(client: "httpx.AsyncClient",
                              access_token: str, p: "_Provider") -> dict:
    # QQ 两步:
    # 1) GET /oauth2.0/me?access_token=...&fmt=json → {client_id, openid}
    # 2) GET /user/get_user_info?access_token+oauth_consumer_key+openid →
    #    {ret, msg, nickname, figureurl, ...}  (无 email — 拼兜底)
    me = await client.get("https://graph.qq.com/oauth2.0/me",
                          params={"access_token": access_token,
                                  "fmt": "json"})
    me.raise_for_status()
    me_data = me.json()
    openid = me_data.get("openid")
    if not openid:
        raise RuntimeError(f"qq no openid in /me response: {me_data}")
    r = await client.get("https://graph.qq.com/user/get_user_info",
                         params={"access_token": access_token,
                                 "oauth_consumer_key": p.client_id,
                                 "openid": openid})
    r.raise_for_status()
    j = r.json()
    if j.get("ret", 0) != 0:
        raise RuntimeError(f"qq user_info ret!=0: {j}")
    # parse_user 用这个 openid 当 sub, 塞回 dict
    j["openid"] = openid
    return j


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


def _parse_feishu(j: dict) -> tuple[str, str | None, str | None]:
    # 飞书 /suite/passport/oauth/userinfo 返 OIDC 风格: sub / name / email /
    # picture / open_id / union_id. sub 优先, 没就 union_id, 再没就 open_id.
    sub = str(j.get("sub") or j.get("union_id") or j.get("open_id") or "")
    email = j.get("email")
    display = j.get("name") or email
    return sub, email, display


def _parse_qq(j: dict) -> tuple[str, str | None, str | None]:
    # QQ user info 不返 email. openid 当 sub, 拼兜底 email.
    sub = str(j.get("openid") or "")
    email = f"qq-{sub[:16]}@qq.users.noreply" if sub else None
    display = j.get("nickname") or (sub[:8] if sub else "QQ user")
    return sub, email, display


def _parse_dingtalk(j: dict) -> tuple[str, str | None, str | None]:
    # 钉钉 /v1.0/contact/users/me 返 {nick, avatarUrl, mobile, openId,
    # unionId, email}. unionId 跨企业稳定, 优先用; email 大概率为空, 用
    # mobile@dingtalk.user 兜底.
    sub = str(j.get("unionId") or j.get("openId") or "")
    email = j.get("email") or (
        f"{j.get('mobile') or sub}@dingtalk.user" if sub else None
    )
    display = j.get("nick")
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
    "qq": _Provider(
        key="qq", label="QQ", color="#12B7F5",
        client_id_env="CCR_HUB_OAUTH_QQ_CLIENT_ID",
        client_secret_env="CCR_HUB_OAUTH_QQ_CLIENT_SECRET",
        authorize_url="https://graph.qq.com/oauth2.0/authorize",
        token_url="https://graph.qq.com/oauth2.0/token",
        userinfo_url="",   # 走 fetch_user_info (两步), userinfo_url 不用
        scope="get_user_info",
        parse_user=_parse_qq,
        build_token_body=_qq_token_body,
        fetch_user_info=_fetch_qq_user_info,
    ),
    "feishu": _Provider(
        key="feishu", label="Feishu", color="#00D6B9",
        client_id_env="CCR_HUB_OAUTH_FEISHU_CLIENT_ID",
        client_secret_env="CCR_HUB_OAUTH_FEISHU_CLIENT_SECRET",
        authorize_url="https://passport.feishu.cn/suite/passport/oauth/authorize",
        token_url="https://passport.feishu.cn/suite/passport/oauth/token",
        userinfo_url="https://passport.feishu.cn/suite/passport/oauth/userinfo",
        scope="openid profile email",
        parse_user=_parse_feishu,
    ),
    "dingtalk": _Provider(
        key="dingtalk", label="DingTalk", color="#1677FF",
        client_id_env="CCR_HUB_OAUTH_DINGTALK_CLIENT_ID",
        client_secret_env="CCR_HUB_OAUTH_DINGTALK_CLIENT_SECRET",
        authorize_url="https://login.dingtalk.com/oauth2/auth",
        token_url="https://api.dingtalk.com/v1.0/oauth2/userAccessToken",
        userinfo_url="https://api.dingtalk.com/v1.0/contact/users/me",
        scope="openid",
        parse_user=_parse_dingtalk,
        token_json=True,
        userinfo_auth="x_acs_dingtalk",
        build_token_body=_dingtalk_token_body,
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
    token_body = p.build_token_body(
        code, _callback_url(provider), p.client_id, p.client_secret,
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = None
        try:
            post_kwargs = {"headers": {"Accept": "application/json"}}
            if p.token_json:
                post_kwargs["json"] = token_body
            else:
                post_kwargs["data"] = token_body
            r = await client.post(p.token_url, **post_kwargs)
            r.raise_for_status()
            tok = r.json()
        except Exception as e:  # noqa: BLE001
            body_preview = ""
            try:
                if r is not None:
                    body_preview = (r.text or "")[:200]
            except Exception:
                pass
            log.warning("[%s] token exchange failed type=%s msg=%r body=%r",
                        provider, type(e).__name__, str(e), body_preview)
            raise HTTPException(
                status_code=502,
                detail=f"token_exchange_failed type={type(e).__name__} msg={e!s} body={body_preview!r}",
            )
        # 钉钉 返 accessToken (camelCase), 其它 access_token (snake_case)
        access_token = tok.get("access_token") or tok.get("accessToken")
        if not access_token:
            log.warning("[%s] no access_token; tok=%r", provider, tok)
            raise HTTPException(
                status_code=502,
                detail=f"no_access_token: {tok}",
            )

        # 拉 userinfo. QQ 这种多步流程走 provider 自定义 fetch_user_info,
        # 其它 provider 走默认 GET userinfo_url + Authorization.
        try:
            if p.fetch_user_info is not None:
                user_json = await p.fetch_user_info(client, access_token, p)
            else:
                if p.userinfo_auth == "x_acs_dingtalk":
                    hdrs = {
                        "x-acs-dingtalk-access-token": access_token,
                        **p.userinfo_headers,
                    }
                else:
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
