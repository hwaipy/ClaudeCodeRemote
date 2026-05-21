"""M-Hub-1 验收: HTTP 透传.

布局:
  Hub 跑在端口 H, 已 seed admin + 一个 device_token
  App 跑在端口 A, 配置 CCR_HUB_URL=ws://hub-H, CCR_HUB_DEVICE_TOKEN=...
  App 启动后自动连 hub, 握手完成 → app online

测试:
  user 用 admin cookie 调 hub 的 GET /api/sessions
    → hub forward 给 online app
    → app 返回 sessions JSON (M-Hub-0 测试用 fixture 空 db, 返回 [])
    → hub 透回 user
  断言: 拿到 [] 跟 app 端直接拿到的一致
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_url(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code < 500:
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(f"server not ready in {timeout}s: {last_err}")


def _wait_app_online(hub_url: str, admin_email: str, admin_pw: str,
                     timeout: float = 10.0) -> dict:
    """轮询 /api/hub/apps 直到有 app online=true."""
    with httpx.Client(base_url=hub_url, timeout=5) as c:
        c.post("/api/hub/login", json={
            "email": admin_email, "password": admin_pw,
        }).raise_for_status()
        deadline = time.time() + timeout
        while time.time() < deadline:
            apps = c.get("/api/hub/apps").json()
            online = [a for a in apps if a["online"]]
            if online:
                return online[0]
            time.sleep(0.1)
    raise RuntimeError("no app went online within timeout")


@pytest.fixture(scope="session")
def hub_and_app():
    """起 hub + 一个连过来的 app, 都用临时 db. yield {hub_url, app_id, ...}."""
    db_dir = Path(tempfile.mkdtemp(prefix="ccr_hubapp_"))
    hub_db = db_dir / "hub.db"
    app_db = db_dir / "app.sqlite"
    hub_port = _free_port()
    app_port = _free_port()
    admin_email = "test@example.com"
    admin_pw = "test-password"
    device_token = "dev-m1-token-12345"

    hub_env = os.environ.copy()
    hub_env.update({
        "CCR_HUB_DB": str(hub_db),
        "CCR_HUB_ADMIN_EMAIL": admin_email,
        "CCR_HUB_ADMIN_PW": admin_pw,
        "CCR_HUB_SEED_DEVICE_TOKEN": device_token,
        "CCR_HUB_SEED_APP_NAME": "forwardable-app",
    })
    hub_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "claude_code_remote.hub.main:app",
         "--host", "127.0.0.1", "--port", str(hub_port),
         "--log-level", "warning"],
        env=hub_env, cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait_url(f"http://127.0.0.1:{hub_port}/healthz", timeout=15)
    except Exception:
        hub_proc.terminate()
        out = hub_proc.stdout.read().decode(errors="replace") if hub_proc.stdout else ""
        raise RuntimeError(f"hub failed: {out}")

    # 起 app, 让它连 hub
    app_env = os.environ.copy()
    app_env.update({
        "CCR_DB_PATH": str(app_db),
        "CCR_TOKEN": "app-local-token",
        "CCR_HUB_URL": f"ws://127.0.0.1:{hub_port}",
        "CCR_HUB_DEVICE_TOKEN": device_token,
        "CCR_HUB_APP_NAME": "forwardable-app",
    })
    app_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "claude_code_remote.server.main:app",
         "--host", "127.0.0.1", "--port", str(app_port),
         "--log-level", "warning"],
        env=app_env, cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait_url(f"http://127.0.0.1:{app_port}/healthz", timeout=15)
        online_app = _wait_app_online(
            f"http://127.0.0.1:{hub_port}", admin_email, admin_pw,
        )
    except Exception:
        app_proc.terminate()
        hub_proc.terminate()
        out = app_proc.stdout.read().decode(errors="replace") if app_proc.stdout else ""
        raise RuntimeError(f"app failed to come online via hub: {out}")

    yield {
        "hub_url": f"http://127.0.0.1:{hub_port}",
        "app_url": f"http://127.0.0.1:{app_port}",
        "app_id": online_app["id"],
        "app_token": "app-local-token",
        "admin_email": admin_email,
        "admin_pw": admin_pw,
    }

    for p in (app_proc, hub_proc):
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


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
