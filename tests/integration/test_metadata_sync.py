"""M-Hub-2: sessions_cache metadata sync.

App 端 (hub_client) 推 sessions snapshot + delta. Hub 端 tunnel 收 control 帧
写 sessions_cache. GET /api/sessions 由 hub 自己处理 (不再 forward), 返回该
user 所有 apps 的合并 list, 每条带 app_id / app_name / app_online.
"""
from __future__ import annotations

import time

import httpx
import pytest


def _login(hub_url, email, pw):
    c = httpx.Client(base_url=hub_url, timeout=5)
    c.post("/api/hub/login", json={"email": email, "password": pw}).raise_for_status()
    return c


def _wait_for_session_in_hub(c: httpx.Client, sid: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get("/api/sessions")
        if r.status_code == 200:
            sessions = r.json()
            match = [s for s in sessions if s.get("id") == sid]
            if match:
                return match[0]
        time.sleep(0.1)
    raise AssertionError(f"sid {sid} did not appear in hub sessions cache")


def test_sessions_cache_contains_app_id_app_name_online(hub_and_app):
    """spawn via hub → hub /api/sessions 应含该 session, 带 app_id / app_name / app_online."""
    c = _login(hub_and_app["hub_url"],
               hub_and_app["admin_email"], hub_and_app["admin_pw"])
    try:
        r = c.post("/api/spawn", json={
            "cwd": hub_and_app["default_cwd"],
            "name": "meta-test-1",
            "permission_mode": "manual",
            "model": "",
            "effort": "",
        })
        assert r.status_code == 200, r.text
        sid = r.json()["id"]

        s = _wait_for_session_in_hub(c, sid)
        assert s["app_id"] == hub_and_app["app_id"], s
        assert s["app_name"] == "forwardable-app", s
        assert s["app_online"] is True, s
        assert s["name"] == "meta-test-1", s
    finally:
        c.close()


def test_sessions_cache_removes_on_delete(hub_and_app):
    """delete via hub → hub /api/sessions 该 session 消失."""
    c = _login(hub_and_app["hub_url"],
               hub_and_app["admin_email"], hub_and_app["admin_pw"])
    try:
        r = c.post("/api/spawn", json={
            "cwd": hub_and_app["default_cwd"],
            "name": "meta-test-del",
            "permission_mode": "manual",
            "model": "",
            "effort": "",
        })
        sid = r.json()["id"]
        _wait_for_session_in_hub(c, sid)

        # delete
        r = c.delete(f"/api/sessions/{sid}")
        assert r.status_code in (200, 204), r.text

        # 等 hub cache 反映
        deadline = time.time() + 5
        while time.time() < deadline:
            sessions = c.get("/api/sessions").json()
            if not any(s["id"] == sid for s in sessions):
                break
            time.sleep(0.1)
        else:
            sessions = c.get("/api/sessions").json()
            assert not any(s["id"] == sid for s in sessions), (
                f"sid {sid} 未从 hub cache 移除: {sessions}"
            )
    finally:
        c.close()


def test_sessions_cache_renames_propagate(hub_and_app):
    """rename via hub → hub /api/sessions 的 name 字段同步."""
    c = _login(hub_and_app["hub_url"],
               hub_and_app["admin_email"], hub_and_app["admin_pw"])
    try:
        r = c.post("/api/spawn", json={
            "cwd": hub_and_app["default_cwd"],
            "name": "rename-before",
            "permission_mode": "manual",
            "model": "",
            "effort": "",
        })
        sid = r.json()["id"]
        _wait_for_session_in_hub(c, sid)

        # rename
        r = c.put(f"/api/sessions/{sid}/rename", json={"name": "rename-after"})
        assert r.status_code == 200, r.text

        deadline = time.time() + 5
        while time.time() < deadline:
            sessions = c.get("/api/sessions").json()
            match = [s for s in sessions if s["id"] == sid]
            if match and match[0]["name"] == "rename-after":
                break
            time.sleep(0.1)
        else:
            assert False, (
                f"rename 没同步到 hub cache: {match if match else 'gone'}"
            )
    finally:
        c.close()
