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

from fastapi import FastAPI

from . import db as hub_db
from .api import router as api_router
from .forwarder import ForwardMiddleware
from .tunnel import router as tunnel_router, registry

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
# Forward middleware 必须在 router 注册后加 (它兜底未匹配的路径)
app.add_middleware(ForwardMiddleware)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "online_apps": len(registry.online_apps())}
