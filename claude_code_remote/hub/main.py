"""Hub FastAPI app — entry point.

启动:
    CCR_HUB_DB=hub.db CCR_HUB_ADMIN_EMAIL=... CCR_HUB_ADMIN_PW=... \
        python3 -m uvicorn claude_code_remote.hub.main:app \
        --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from fastapi import Cookie
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import server as app_server_pkg
from ..server import config as app_config
from . import db as hub_db
from .api import me_handler, router as api_router
from .forwarder import ForwardMiddleware
from .tunnel import router as tunnel_router, registry
from .ws_forwarder import router as ws_forwarder_router

STATIC_DIR = Path(app_server_pkg.__file__).parent / "static"


def _render_html(text: str) -> str:
    """跟 server/main.py 同款占位符替换 — __BUILD_ID__ 走文件 mtime,
    __ROOT__ 留空 (Hub 不走子路径, 始终 root)."""
    return (text
            .replace("__BUILD_ID__", app_config._build_id())
            .replace("__ROOT__", ""))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ccr.hub")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db_path = os.environ.get("CCR_HUB_DB", "hub.db")
    admin_email = os.environ.get("CCR_HUB_ADMIN_EMAIL")
    admin_pw = os.environ.get("CCR_HUB_ADMIN_PW")
    await hub_db.init(db_path)
    if admin_email and admin_pw:
        await hub_db.ensure_admin(admin_email, admin_pw)
    # M-Hub-0 测试用: 一个固定 device_token + app 注册. 后期由 pairing 流程替代.
    seed_token = os.environ.get("CCR_HUB_SEED_DEVICE_TOKEN")
    seed_name = os.environ.get("CCR_HUB_SEED_APP_NAME")
    if seed_token and seed_name and admin_email:
        await hub_db.ensure_seed_app(admin_email, seed_name, seed_token)
    log.info("hub started db=%s", db_path)
    yield
    log.info("hub stopped")


app = FastAPI(lifespan=lifespan)
app.include_router(api_router)
app.include_router(tunnel_router)
app.include_router(ws_forwarder_router)
# Forward middleware 必须在 router 注册后加 (它兜底未匹配的 HTTP 路径).
# WebSocket scope 不走该 middleware (BaseHTTPMiddleware 只接 http scope).
app.add_middleware(ForwardMiddleware)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "online_apps": len(registry.online_apps())}


@app.get("/api/me")
async def get_me(ccr_sess: str | None = Cookie(None)):
    return await me_handler(ccr_sess)


# ---- Static SPA (跟 app 端共用一份 static, html=True 自动 fallback 到 index.html) ----

@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(
        _render_html((STATIC_DIR / "index.html").read_text(encoding="utf-8")),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/icon.svg")
async def icon() -> FileResponse:
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/sw.js")
async def sw() -> Response:
    return Response(
        _render_html((STATIC_DIR / "sw.js").read_text(encoding="utf-8")),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/",
                 "Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/manifest.webmanifest")
async def manifest() -> Response:
    return Response(
        _render_html((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8")),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# 兜底 — /static 下面是 app.js / style.css 等
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="hub-static")
