"""M-Hub-1 验收: HTTP 透传.

用 conftest.py 的 hub_and_app fixture: 起 hub + app, app 走反向 WS 连过来,
用 fake_claude 当 claude binary.
"""
from __future__ import annotations

import httpx


def test_app_online_via_tunnel(hub_and_app):
    assert hub_and_app["app_id"].startswith("app-")


def test_forward_get_sessions_returns_json(hub_and_app):
    """hub forward GET /api/sessions → app return [] (空 db) → hub 透回."""
    with httpx.Client(base_url=hub_and_app["hub_url"], timeout=10) as c:
        c.post("/api/hub/login", json={
            "email": hub_and_app["admin_email"],
            "password": hub_and_app["admin_pw"],
        }).raise_for_status()
        r = c.get("/api/sessions")
        assert r.status_code == 200, r.text
        via_hub = r.json()
        # 跟 app 直接拿到的应一致 (内容字符级相同, 不论 dict / list shape)
        with httpx.Client(base_url=hub_and_app["app_url"],
                          headers={"Authorization":
                                   f"Bearer {hub_and_app['app_token']}"},
                          timeout=5) as ac:
            r2 = ac.get("/api/sessions")
            assert r2.status_code == 200
            assert r2.json() == via_hub


def test_forward_404_passthrough(hub_and_app):
    """app 端不存在的路径, hub forward 后也应得 app 的 404 (不是 hub 自己 404)."""
    with httpx.Client(base_url=hub_and_app["hub_url"], timeout=10) as c:
        c.post("/api/hub/login", json={
            "email": hub_and_app["admin_email"],
            "password": hub_and_app["admin_pw"],
        }).raise_for_status()
        r = c.get("/api/nonexistent-route")
        assert r.status_code == 404


def test_forward_requires_auth(hub_and_app):
    """没登录的 user 调 hub /api/sessions → 401, 不该 forward 给 app."""
    with httpx.Client(base_url=hub_and_app["hub_url"], timeout=5) as c:
        r = c.get("/api/sessions")
        assert r.status_code in (401, 403)
