"""FastAPI app 拼装。

启动：
    CCR_TOKEN=$(openssl rand -hex 16) python3 -m uvicorn server.main:app \
        --host 0.0.0.0 --port 1881
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .api import router as api_router
from .session_manager import manager
from .ws import router as ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await manager.startup()
    try:
        yield
    finally:
        await manager.shutdown()
        await db.close()


app = FastAPI(title="ClaudeCodeRemote", lifespan=lifespan)
app.include_router(api_router)
app.include_router(ws_router)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


def _render(text: str) -> str:
    return (text
            .replace("__BUILD_ID__", config.BUILD_ID)
            .replace("__ROOT__", config.ROOT_PATH))


_INDEX_HTML = _render((config.STATIC_DIR / "index.html").read_text(encoding="utf-8"))
_SW_JS      = _render((config.STATIC_DIR / "sw.js").read_text(encoding="utf-8"))
_MANIFEST   = _render((config.STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))


@app.get("/")
async def index() -> "HTMLResponse":
    # 静态资源带 ?v=<BUILD_ID> 让浏览器强缓存按文件变更自动失效
    return HTMLResponse(
        _INDEX_HTML,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# PWA 三件套在根路径（或 ROOT_PATH 下）暴露
from fastapi.responses import FileResponse, Response


@app.get("/manifest.webmanifest")
async def pwa_manifest() -> "Response":
    return Response(
        _MANIFEST,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/icon.svg")
async def pwa_icon() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/sw.js")
async def pwa_sw() -> Response:
    return Response(
        _SW_JS,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": (config.ROOT_PATH or "") + "/",
            "Cache-Control": "no-cache, must-revalidate",
        },
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
