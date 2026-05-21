"""HTTP forwarder middleware — 把 user 请求透给反向 WS 上的 app.

ccr-hub-spec.html §3 §5:
- 路径 /api/hub/* / /healthz / static 资源 → Hub 自处理
- 其它 /api/* → 找 online app → tunnel.send_http_req → 透回响应
- M-Hub-1 简化: 一个用户只有一个 online app 时直接转给它. M-Hub-2 起按
  目标 sid 查 sessions_cache 决定路由.
"""
from __future__ import annotations

import base64
import logging
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..shared import tunnel_proto as tp
from . import auth, db as hub_db
from .tunnel import registry

log = logging.getLogger("ccr.hub.forwarder")

# 不 forward 的路径前缀 (Hub 自己处理)
_HUB_LOCAL_PREFIXES = ("/api/hub/", "/healthz", "/app-tunnel")
# 透传 user 请求时不该带过去的 header
_DROP_HEADERS = {
    "host", "connection", "cookie",   # cookie 是 hub 的, 不该给 app
    "authorization",                   # hub-cookie 体系, app 那个 token 跟它无关
    "content-length",                  # 由 body_b64 决定
}


def _is_local_path(path: str) -> bool:
    if path == "/" or path == "":
        return True
    return any(path.startswith(p) for p in _HUB_LOCAL_PREFIXES)


async def _pick_app_for_user(user_id: str):
    """M-Hub-1: 简单挑该 user 的第一个 online app. M-Hub-2 按 sid 路由."""
    for online in registry.online_apps():
        if online.user_id == user_id:
            return online
    return None


class ForwardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_local_path(path):
            return await call_next(request)

        # 需要 user identity. 复用 hub cookie auth (api.py 里的 cookie 名).
        cookie = request.cookies.get("ccr_sess")
        user_id = auth.get_user_id(cookie)
        if not user_id:
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401,
            )

        # GET /api/sessions: 走 hub 的 sessions_cache (聚合该 user 所有 apps).
        # 不再 forward — M-Hub-2: home view 看到合并 list, 每条带 app_id 等.
        if request.method == "GET" and path == "/api/sessions":
            sessions = await hub_db.list_user_sessions(user_id)
            online_set = set(registry.online_app_ids())
            apps = await hub_db.list_apps_for_user(user_id)
            app_name_by_id = {a["id"]: a["name"] for a in apps}
            out = []
            for s in sessions:
                out.append({
                    "id": s["sid"],
                    "name": s.get("name") or "",
                    "cwd": s.get("cwd") or "",
                    "state": s.get("state"),
                    "last_activity_at": s.get("last_active"),
                    "created_at": s.get("created_at"),
                    "model": s.get("model") or "",
                    "effort": s.get("effort") or "",
                    "permission_mode": s.get("permission_mode"),
                    "app_id": s["app_id"],
                    "app_name": s.get("app_name")
                                 or app_name_by_id.get(s["app_id"], ""),
                    "app_online": s["app_id"] in online_set,
                })
            return JSONResponse(out)

        online = await _pick_app_for_user(user_id)
        if not online:
            return JSONResponse(
                {"error": "app_offline",
                 "message": "no online app for this user"},
                status_code=503,
            )

        # 构造 HttpReq
        body = await request.body()
        headers = {}
        for k, v in request.headers.items():
            if k.lower() in _DROP_HEADERS:
                continue
            headers[k] = v
        # 给 app 端 user identity
        headers["x-ccr-user-id"] = user_id

        req = tp.HttpReq(
            stream_id="r-" + secrets.token_hex(8),
            method=request.method,
            path=path,
            query=request.url.query,
            headers=headers,
            body_b64=base64.b64encode(body).decode("ascii"),
            user_id=user_id,
        )
        try:
            res = await online.send_http_req(req, timeout=30.0)
        except Exception as e:  # noqa: BLE001
            log.warning("forward %s %s failed: %s", request.method, path, e)
            return JSONResponse(
                {"error": "forward_failed", "detail": str(e)},
                status_code=502,
            )
        await hub_db.touch_app_seen(online.app_id)

        # 透回响应
        body_bytes = base64.b64decode(res.body_b64) if res.body_b64 else b""
        out_headers = {}
        for k, v in res.headers.items():
            if k.lower() in {"transfer-encoding", "content-length",
                              "connection"}:
                continue
            out_headers[k] = v
        return Response(
            content=body_bytes,
            status_code=res.status,
            headers=out_headers,
            media_type=out_headers.get("content-type"),
        )
