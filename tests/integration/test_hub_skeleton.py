"""M-Hub-0 验收测试 (协议骨架 — 单元层).

Hub 起来后:
- GET /healthz 200
- GET /api/hub/apps 没登录返 401, 登录后返 list (含 seed 的 test-app)
- POST /api/hub/login 用 admin 凭据 → 设 cookie
- WS /app-tunnel?token=... 接受 hello → 回 ready → app 出现在 apps 列表 online
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from claude_code_remote.shared import tunnel_proto as tp


def test_hub_healthz(hub_env):
    r = httpx.get(f"{hub_env['base_url']}/healthz", timeout=5)
    assert r.status_code == 200


def test_hub_apps_requires_auth(hub_env):
    r = httpx.get(f"{hub_env['base_url']}/api/hub/apps", timeout=5)
    assert r.status_code in (401, 403), (
        f"未登录 /api/hub/apps 应返 401/403, got {r.status_code}"
    )


def test_hub_login_and_list_apps(hub_env):
    """admin login → list apps 应含 seed 的 test-app, online=false (尚未连 ws)."""
    with httpx.Client(base_url=hub_env["base_url"], timeout=5) as c:
        r = c.post("/api/hub/login", json={
            "email": hub_env["admin_email"],
            "password": hub_env["admin_pw"],
        })
        assert r.status_code == 200, r.text
        r = c.get("/api/hub/apps")
        assert r.status_code == 200, r.text
        apps = r.json()
        assert isinstance(apps, list)
        names = [a["name"] for a in apps]
        assert "test-app" in names, names
        seed = [a for a in apps if a["name"] == "test-app"][0]
        assert seed["online"] is False


@pytest.mark.anyio
async def test_hub_app_tunnel_handshake(hub_env):
    """App 连 WS → 发 hello → 收 ready → /api/hub/apps 该 app online=true."""
    import websockets

    ws_url = (
        f"{hub_env['ws_url']}/app-tunnel"
        f"?token={hub_env['device_token']}"
    )
    async with websockets.connect(ws_url) as ws:
        # hello
        hello = tp.Control(
            stream_id="*",
            op="hello",
            data={
                "app_name": "test-app",
                "version": tp.TUNNEL_PROTO_VERSION,
                "capabilities": [],
            },
        )
        await ws.send(tp.encode(hello))
        # ready
        raw = await asyncio.wait_for(ws.recv(), timeout=3)
        ack = tp.decode(raw)
        assert isinstance(ack, tp.Control), type(ack).__name__
        assert ack.op == "ready", ack
        assert "app_id" in ack.data
        assert "user_id" in ack.data

        # 等 hub 标 online (异步, 给一拍)
        await asyncio.sleep(0.1)

        # 用同一个 admin cookie 查 apps, test-app 应 online
        async with httpx.AsyncClient(
            base_url=hub_env["base_url"], timeout=5,
        ) as c:
            r = await c.post("/api/hub/login", json={
                "email": hub_env["admin_email"],
                "password": hub_env["admin_pw"],
            })
            assert r.status_code == 200
            r = await c.get("/api/hub/apps")
            apps = r.json()
            seed = [a for a in apps if a["name"] == "test-app"][0]
            assert seed["online"] is True, apps


@pytest.mark.anyio
async def test_hub_rejects_bad_token(hub_env):
    """带不对的 token 连 ws → 立刻 close."""
    import websockets

    ws_url = f"{hub_env['ws_url']}/app-tunnel?token=garbage"
    with pytest.raises(websockets.exceptions.WebSocketException):
        async with websockets.connect(ws_url) as ws:
            await asyncio.wait_for(ws.recv(), timeout=2)
