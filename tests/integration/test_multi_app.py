"""M-Hub-5: 多 app 合并 + offline 容错.

- 同一 user 下 2 个 apps, 都 spawn 一个 session
- hub /api/sessions 返合并 list, 排序 by last_active desc, 每条带 app_id
- 杀 app-A 进程 → hub apps[A].online=false, 对应 sessions app_online=false
- spawn 时显式指定 app_id → forward 到正确 app
"""
from __future__ import annotations

import time

import httpx


def _login(hub_url, email, pw):
    c = httpx.Client(base_url=hub_url, timeout=5)
    c.post("/api/hub/login", json={"email": email, "password": pw}).raise_for_status()
    return c


def test_two_apps_sessions_merged(multi_app_hub):
    """spawn 2 sessions on 2 apps → hub /api/sessions 看到合并 list."""
    env = multi_app_hub
    c = _login(env["hub_url"], env["admin_email"], env["admin_pw"])
    try:
        r = c.post("/api/spawn", json={
            "cwd": env["app_a"]["cwd"], "name": "on-app-A",
            "permission_mode": "manual", "model": "", "effort": "",
            "app_id": env["app_a"]["id"],
        })
        assert r.status_code == 200, r.text
        sid_a = r.json()["id"]

        r = c.post("/api/spawn", json={
            "cwd": env["app_b"]["cwd"], "name": "on-app-B",
            "permission_mode": "manual", "model": "", "effort": "",
            "app_id": env["app_b"]["id"],
        })
        assert r.status_code == 200, r.text
        sid_b = r.json()["id"]

        # 等两条都进 cache
        deadline = time.time() + 5
        while time.time() < deadline:
            sessions = c.get("/api/sessions").json()
            by_sid = {s["id"]: s for s in sessions}
            if sid_a in by_sid and sid_b in by_sid:
                break
            time.sleep(0.1)
        else:
            assert False, f"missing sessions in cache: {sessions}"

        assert by_sid[sid_a]["app_id"] == env["app_a"]["id"]
        assert by_sid[sid_a]["app_name"] == "app-A"
        assert by_sid[sid_a]["app_online"] is True
        assert by_sid[sid_b]["app_id"] == env["app_b"]["id"]
        assert by_sid[sid_b]["app_name"] == "app-B"
        assert by_sid[sid_b]["app_online"] is True
    finally:
        c.close()


def test_kill_app_reflects_offline(multi_app_hub):
    """杀掉 app-A → hub /api/sessions 该 session app_online 转 false."""
    env = multi_app_hub
    c = _login(env["hub_url"], env["admin_email"], env["admin_pw"])
    try:
        r = c.post("/api/spawn", json={
            "cwd": env["app_a"]["cwd"], "name": "on-A-kill",
            "permission_mode": "manual", "model": "", "effort": "",
            "app_id": env["app_a"]["id"],
        })
        sid = r.json()["id"]

        # 等条目进 cache
        deadline = time.time() + 5
        while time.time() < deadline:
            sessions = c.get("/api/sessions").json()
            match = [s for s in sessions if s["id"] == sid]
            if match and match[0]["app_online"]:
                break
            time.sleep(0.1)

        # kill app-A
        env["app_a"]["proc"].terminate()
        env["app_a"]["proc"].wait(timeout=3)

        # 轮询直到 app_online=false
        deadline = time.time() + 5
        while time.time() < deadline:
            sessions = c.get("/api/sessions").json()
            match = [s for s in sessions if s["id"] == sid]
            if match and match[0]["app_online"] is False:
                return
            time.sleep(0.1)
        assert False, f"app-A kill 后 app_online 应转 false: {match}"
    finally:
        c.close()


def test_spawn_to_offline_app_returns_503(multi_app_hub):
    """杀 app-A 后 spawn 指定 app_id=A → 503."""
    env = multi_app_hub
    c = _login(env["hub_url"], env["admin_email"], env["admin_pw"])
    try:
        env["app_a"]["proc"].terminate()
        env["app_a"]["proc"].wait(timeout=3)
        # 等 hub 注意到 (registry remove 是 async, 等握手 lost)
        time.sleep(0.5)

        r = c.post("/api/spawn", json={
            "cwd": env["app_a"]["cwd"], "name": "should-fail",
            "permission_mode": "manual", "model": "", "effort": "",
            "app_id": env["app_a"]["id"],
        })
        assert r.status_code == 503, r.text
    finally:
        c.close()
