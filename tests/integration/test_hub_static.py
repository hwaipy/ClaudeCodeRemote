"""M-Hub-4 Phase 1: Hub static + /api/me.

- Hub `GET /` 返 SPA HTML (跟 app 同款)
- `GET /api/me` 区分 mode:
    hub:   {mode:"hub", user_id, apps: [...]}
    local: {mode:"local"} (app 直连时, 浏览器 SPA 看到这个就走 local)
"""
from __future__ import annotations

import httpx


def test_hub_serves_spa_root(hub_env):
    """Hub 根路径返 SPA HTML."""
    r = httpx.get(hub_env["base_url"] + "/", timeout=5)
    assert r.status_code == 200, r.text
    # 至少含 SPA 关键元素
    text = r.text
    assert "<html" in text.lower() and "</html>" in text.lower()


def test_hub_me_endpoint_anon(hub_env):
    """匿名 /api/me 应表示未登录 (mode=hub, user_id=null)."""
    r = httpx.get(hub_env["base_url"] + "/api/me", timeout=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("mode") == "hub", body
    assert body.get("user_id") is None, body


def test_hub_me_endpoint_logged_in(hub_env):
    """登录后 /api/me 返 user_id + apps list."""
    with httpx.Client(base_url=hub_env["base_url"], timeout=5) as c:
        c.post("/api/hub/login", json={
            "email": hub_env["admin_email"],
            "password": hub_env["admin_pw"],
        }).raise_for_status()
        body = c.get("/api/me").json()
        assert body["mode"] == "hub"
        assert body["user_id"].startswith("user-")
        assert isinstance(body["apps"], list)


def test_app_local_me_endpoint(hub_and_app):
    """App 直连时 /api/me 返 mode=local."""
    r = httpx.get(
        hub_and_app["app_url"] + "/api/me",
        headers={"Authorization": f"Bearer {hub_and_app['app_token']}"},
        timeout=5,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("mode") == "local", body
