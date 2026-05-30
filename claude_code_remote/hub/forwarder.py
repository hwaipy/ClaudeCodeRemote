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
_HUB_LOCAL_PREFIXES = (
    "/api/hub/", "/api/me",
    "/healthz", "/app-tunnel",
    "/static/", "/sw.js", "/icon.svg", "/manifest.webmanifest",
)
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
    """M-Hub-1 兜底: 该 user 第一个 online app."""
    for online in registry.online_apps():
        if online.user_id == user_id:
            return online
    return None


async def _pick_app_for_sid(user_id: str, sid: str):
    """按 sid 查 sessions_cache 找归属 app, 路由实时请求 (M-Hub-2+)."""
    rows = await hub_db.list_user_sessions(user_id)
    for r in rows:
        if r["sid"] == sid:
            for o in registry.online_apps():
                if o.app_id == r["app_id"]:
                    return o
    return None


async def _resolve_target(request, user_id: str):
    """根据 path / method / body 决定 forward 目标 app.

    - POST /api/spawn: body 里如果带 app_id 用它; 否则挑第一个 online
    - /api/sessions/<sid>/...: 按 sid 路由
    - 其它: 第一个 online (兼容老行为)

    返回 (online_app, body_bytes). body 是被 consume 过的, 调用方拿 cached.
    """
    body = await request.body()
    path = request.url.path
    # Spawn: 显式 app_id
    if request.method == "POST" and path == "/api/spawn":
        try:
            import json as _json
            j = _json.loads(body) if body else {}
            req_app_id = (j.get("app_id") or "").strip()
        except Exception:
            req_app_id = ""
        if req_app_id:
            for o in registry.online_apps():
                if o.user_id == user_id and o.app_id == req_app_id:
                    # 给 app 清掉 app_id 字段, 它不需要
                    j.pop("app_id", None)
                    body = _json.dumps(j).encode("utf-8")
                    return o, body
            # 用户明确指定的 app 不 online → 不能 fallback (会 spawn 到错 app)
            return None, body
        return await _pick_app_for_user(user_id), body

    # /api/sessions/<sid>/... or DELETE /api/sessions/<sid>
    parts = path.split("/")
    if len(parts) >= 4 and parts[1] == "api" and parts[2] == "sessions":
        sid = parts[3]
        o = await _pick_app_for_sid(user_id, sid)
        if o is not None:
            return o, body

    # /api/ls 和 /api/mkdir 在 new-session modal 里浏览/创目录用 — 必须
    # 路由到用户在 spawn-app select 里选的 server (不是第一个 online),
    # 否则你选 USTCClaw 但 path browser 显示 UbuntuClaw 的目录, 错位严重.
    # 前端调用时把 app_id 加到 query (?app_id=app-xxx).
    if path in ("/api/ls", "/api/mkdir"):
        req_app_id = (request.query_params.get("app_id") or "").strip()
        if req_app_id:
            for o in registry.online_apps():
                if o.user_id == user_id and o.app_id == req_app_id:
                    return o, body
            # 用户明确指定 app 不 online — fallback first online 避免完全 broken
    return await _pick_app_for_user(user_id), body


async def _handle_public_files(request: Request, path: str) -> Response:
    """GET /files/<short_host>/<fid> → 找在线 app → forward /api/share/<fid>.

    完全公开, 不查 cookie / token. 任何错误统一 404 防嗅探:
    - short_host 不存在 → 404
    - app 离线 → 404 (不告诉攻击者"这台 app 曾经存在")
    - app 端 404 → 透传 404
    """
    parts = path.split("/")
    # ['', 'files', '<sh>', '<fid>', ...rest]
    if len(parts) < 4 or not parts[2] or not parts[3]:
        return Response(status_code=404)
    short_host = parts[2]
    fid = parts[3]

    app_row = await hub_db.find_app_by_short_host(short_host)
    if not app_row:
        return Response(status_code=404)
    online = None
    for o in registry.online_apps():
        if o.app_id == app_row["id"]:
            online = o
            break
    if not online:
        # 不返 503 — 不告诉外人 "这台 app 是注册过的, 只是现在离线"
        return Response(status_code=404)

    # 改写到 app 的 share endpoint
    target_path = f"/api/share/{fid}"
    body = await request.body()
    headers = {}
    for k, v in request.headers.items():
        if k.lower() in _DROP_HEADERS:
            continue
        headers[k] = v
    # 公开 path 不带 x-ccr-user-id (app 端 /api/share/<id> 不需要 owner)

    req = tp.HttpReq(
        stream_id="r-" + secrets.token_hex(8),
        method=request.method,
        path=target_path,
        query=request.url.query,
        headers=headers,
        body_b64=base64.b64encode(body).decode("ascii"),
        user_id="",   # 公开
    )
    try:
        res = await online.send_http_req(req, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        log.warning("/files forward failed: %s", e)
        return Response(status_code=502)
    await hub_db.touch_app_seen(online.app_id)

    body_bytes = base64.b64decode(res.body_b64) if res.body_b64 else b""
    out_headers = {}
    for k, v in res.headers.items():
        if k.lower() in {"transfer-encoding", "content-length", "connection"}:
            continue
        out_headers[k] = v
    return Response(
        content=body_bytes,
        status_code=res.status,
        headers=out_headers,
        media_type=out_headers.get("content-type"),
    )


class ForwardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_local_path(path):
            return await call_next(request)

        # 公开文件分享 /files/<short_host>/<fid> — bypass auth, 按 short_host
        # 查 app, 转发到 app 的 /api/share/<fid>. spec §17.
        if path.startswith("/files/"):
            return await _handle_public_files(request, path)

        # 需要 user identity. 复用 hub cookie auth (api.py 里的 cookie 名).
        cookie = request.cookies.get("ccr_sess")
        user_id = await auth.get_user_id(cookie)
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
                    "cur_model": s.get("cur_model") or "",
                    "permission_mode": s.get("permission_mode"),
                    "is_stash": bool(s.get("is_stash")),
                    "is_inactive": bool(s.get("is_inactive")),
                    "pending_permissions": int(s.get("pending_permissions") or 0),
                    "needs_action_detail": s.get("needs_action_detail"),
                    "seen_at": s.get("seen_at"),
                    "app_id": s["app_id"],
                    "app_name": s.get("app_name")
                                 or app_name_by_id.get(s["app_id"], ""),
                    "app_online": s["app_id"] in online_set,
                })
            return JSONResponse(out)

        online, body = await _resolve_target(request, user_id)
        if not online:
            return JSONResponse(
                {"error": "app_offline",
                 "message": "no online app for this user / sid"},
                status_code=503,
            )

        # body 已被 _resolve_target 读取过 (并可能改了 app_id 字段)
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
