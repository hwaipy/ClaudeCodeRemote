"""M-Hub-3 验收: pairing flow + revoke.

- 已登录 user POST /api/hub/pair → 拿短期 pairing_code
- App (无 cookie) POST /api/hub/pair/redeem {code, app_name} → 拿 device_token
  + app_id + user_id. code 消费一次后过期.
- DELETE /api/hub/apps/<id> → app 被踢, 重连用旧 token 应 reject.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from claude_code_remote.shared import tunnel_proto as tp


def _login(c: httpx.Client, email: str, pw: str) -> None:
    r = c.post("/api/hub/login", json={"email": email, "password": pw})
    r.raise_for_status()


def test_pairing_basic_flow(hub_env):
    """登录 → 生成 pairing code → redeem 拿 device_token → 再 redeem 同 code 应失败."""
    with httpx.Client(base_url=hub_env["base_url"], timeout=5) as c:
        _login(c, hub_env["admin_email"], hub_env["admin_pw"])
        r = c.post("/api/hub/pair")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "code" in body, body
        assert "expires_at" in body, body
        code = body["code"]
        assert isinstance(code, str) and len(code) >= 6, code

    # redeem 不需要登录, 只需要 code (app CLI 场景)
    with httpx.Client(base_url=hub_env["base_url"], timeout=5) as c:
        r = c.post("/api/hub/pair/redeem", json={
            "code": code, "app_name": "test-mac",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("device_token"), body
        assert body.get("app_id", "").startswith("app-"), body
        assert body.get("user_id", "").startswith("user-"), body

        # 同 code 再 redeem 应失败 (consumed)
        r = c.post("/api/hub/pair/redeem", json={
            "code": code, "app_name": "test-mac-2",
        })
        assert r.status_code in (400, 403, 410), r.text


def test_pairing_bad_code_rejected(hub_env):
    with httpx.Client(base_url=hub_env["base_url"], timeout=5) as c:
        r = c.post("/api/hub/pair/redeem", json={
            "code": "000000-bogus", "app_name": "x",
        })
        assert r.status_code in (400, 403, 404), r.text


@pytest.mark.anyio
async def test_revoke_kills_app_tunnel(hub_env):
    """DELETE /api/hub/apps/<id> → token revoke. 重连 ws 应 reject."""
    import websockets

    # 先 redeem 一个全新 pairing → 拿专门的 device_token, 不影响 seed app
    async with httpx.AsyncClient(base_url=hub_env["base_url"], timeout=5) as c:
        await c.post("/api/hub/login", json={
            "email": hub_env["admin_email"],
            "password": hub_env["admin_pw"],
        })
        pair = (await c.post("/api/hub/pair")).json()
        redeemed = (await c.post("/api/hub/pair/redeem", json={
            "code": pair["code"], "app_name": "revoke-test-app",
        })).json()
        app_id = redeemed["app_id"]
        token = redeemed["device_token"]

        # 先确认 token 能用 (握手通)
        ws_url = f"{hub_env['ws_url']}/app-tunnel?token={token}"
        async with websockets.connect(ws_url) as ws:
            hello = tp.Control(
                stream_id="*", op="hello",
                data={"app_name": "revoke-test-app",
                       "version": tp.TUNNEL_PROTO_VERSION},
            )
            await ws.send(tp.encode(hello))
            raw = await asyncio.wait_for(ws.recv(), timeout=2)
            ack = tp.decode(raw)
            assert isinstance(ack, tp.Control) and ack.op == "ready"

        # revoke
        r = await c.delete(f"/api/hub/apps/{app_id}")
        assert r.status_code in (200, 204), r.text

    # 重连应被拒
    with pytest.raises(Exception):
        async with websockets.connect(
            f"{hub_env['ws_url']}/app-tunnel?token={token}",
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=2)
