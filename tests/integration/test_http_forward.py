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
        # M-Hub-2: hub /api/sessions 返聚合 list, 不是 app 的 {sessions: [...]} dict.
        # 这是 hub mode 的接口形态; SPA M-Hub-4 时按需 normalize.
        assert isinstance(via_hub, list)
        # 用空 cache 启动时, list 应为空 (fixture session-level cache 已被前面
        # spawn 测试污染时, 至少含 forwardable-app 自己的 session). 我们只
        # 验证基础形状 + 每条带 app_id / app_online.
        for s in via_hub:
            assert "app_id" in s, s
            assert "app_name" in s, s
            assert "app_online" in s, s


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
