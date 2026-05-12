"""Bearer token 鉴权。HTTP 走 Authorization 头，WS 走 ?token= query。"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from . import config


def _safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def require_token(request: Request) -> None:
    """FastAPI Depends：HTTP 鉴权。"""
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
