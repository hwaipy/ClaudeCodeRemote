"""Bearer token 鉴权。HTTP 走 Authorization 头，WS 走 ?token= query。"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from . import config


def _safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _is_via_hub(request: Request) -> bool:
    """是否由 hub_client 内部 ASGI dispatch 进来的请求.

    hub_client._dispatch_http 在 scope["state"]["via_hub"] = True; ASGI app
    看到这个标记 → 信 Hub 已 auth 过 (X-CCR-User-Id header), 跳过本地 bearer
    校验. 外部 HTTP 走真 TCP 进来时, ASGI 自己创建的 scope 没这个 state, 不
    存在被伪造的可能.
    """
    return bool(request.scope.get("state", {}).get("via_hub"))


def require_token(request: Request) -> None:
    """FastAPI Depends：HTTP 鉴权."""
    if _is_via_hub(request):
        return   # Hub 已 auth, 信任
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        candidate = auth.split(" ", 1)[1].strip()
        if _safe_eq(candidate, config.TOKEN):
            return
    # 备选：?token= 查询串（前端 SW / iframe 等场景）
    qs_token = request.query_params.get("token", "")
    if qs_token and _safe_eq(qs_token, config.TOKEN):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid bearer token",
    )


def check_ws_token(token: str | None) -> bool:
    if not token:
        return False
    return _safe_eq(token, config.TOKEN)
