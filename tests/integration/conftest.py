"""Integration fixtures for Hub + App combined tests.

Hub server 在 subprocess 跑 (跟 tests/conftest.py 起 app 同款方式),
App 端 hub_client 通过 fixture instance 直接驱动 (避免再起一个 subprocess).
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# anyio backend — 用 asyncio (默认), 不开 trio
@pytest.fixture
def anyio_backend():
    return "asyncio"


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


@pytest.fixture(scope="session")
def hub_env():
    """Boot a Hub server on a random port in subprocess. Cleanup on teardown."""
    port = _free_port()
    db_dir = Path(tempfile.mkdtemp(prefix="ccr_hub_"))
    db_path = db_dir / "hub.db"
    admin_email = "test@example.com"
    admin_pw = "test-password"
    device_token = "dev-" + secrets.token_hex(12)

    env = os.environ.copy()
    env["CCR_HUB_DB"] = str(db_path)
    env["CCR_HUB_BIND"] = f"127.0.0.1:{port}"
    env["CCR_HUB_ADMIN_EMAIL"] = admin_email
    env["CCR_HUB_ADMIN_PW"] = admin_pw
    # 测试用固定 device_token 走捷径 (M-Hub-0 还没接 pairing 流程)
    env["CCR_HUB_SEED_DEVICE_TOKEN"] = device_token
    env["CCR_HUB_SEED_APP_NAME"] = "test-app"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "claude_code_remote.hub.main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        env=env,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_url(f"http://127.0.0.1:{port}/healthz", timeout=15.0)
    except Exception:
        proc.terminate()
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        raise RuntimeError(f"Hub failed to start. logs:\n{out}")

    yield {
        "port": port,
        "base_url": f"http://127.0.0.1:{port}",
        "ws_url": f"ws://127.0.0.1:{port}",
        "db_path": db_path,
        "admin_email": admin_email,
        "admin_pw": admin_pw,
        "device_token": device_token,
    }

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


FAKE_CLAUDE = PROJECT_ROOT / "tests" / "fakes" / "fake_claude.py"


def _wait_app_online(hub_url: str, admin_email: str, admin_pw: str,
                     timeout: float = 10.0) -> dict:
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
    """起 hub + 一个连过来的 app, fake_claude 当 claude binary.

    用于 M-Hub-1+ 的 HTTP/WS forward 测试. App 走反向 WS 连 hub, spawn /
    ws 等动作经 hub 透到 app, 测试只跟 hub 说话.
    """
    db_dir = Path(tempfile.mkdtemp(prefix="ccr_hubapp_"))
    hub_db = db_dir / "hub.db"
    app_db = db_dir / "app.sqlite"
    default_cwd = tempfile.mkdtemp(prefix="ccr-hubapp-cwd-")
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

    base_env = {k: v for k, v in os.environ.items() if not k.startswith("CCR_")}
    app_env = {
        **base_env,
        "CCR_DB_PATH": str(app_db),
        "CCR_DEFAULT_CWD": default_cwd,
        "CCR_TOKEN": "app-local-token",
        "CCR_HOST": "127.0.0.1",
        "CCR_PORT": str(app_port),
        "CCR_ROOT_PATH": "",
        "CCR_CLAUDE_BIN": str(FAKE_CLAUDE),
        "CCR_HUB_URL": f"ws://127.0.0.1:{hub_port}",
        "CCR_HUB_DEVICE_TOKEN": device_token,
        "CCR_HUB_APP_NAME": "forwardable-app",
        "PYTHONUNBUFFERED": "1",
    }
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
        "ws_hub_url": f"ws://127.0.0.1:{hub_port}",
        "app_url": f"http://127.0.0.1:{app_port}",
        "app_id": online_app["id"],
        "app_token": "app-local-token",
        "admin_email": admin_email,
        "admin_pw": admin_pw,
        "default_cwd": default_cwd,
    }

    for p in (app_proc, hub_proc):
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


def _spawn_app(hub_base_url: str, device_token: str, app_name: str,
               app_db_path: str, default_cwd: str, app_port: int,
               ) -> subprocess.Popen:
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("CCR_")}
    app_env = {
        **base_env,
        "CCR_DB_PATH": app_db_path,
        "CCR_DEFAULT_CWD": default_cwd,
        "CCR_TOKEN": f"local-{secrets.token_hex(4)}",
        "CCR_HOST": "127.0.0.1",
        "CCR_PORT": str(app_port),
        "CCR_ROOT_PATH": "",
        "CCR_CLAUDE_BIN": str(FAKE_CLAUDE),
        "CCR_HUB_URL": hub_base_url.replace("http://", "ws://"),
        "CCR_HUB_DEVICE_TOKEN": device_token,
        "CCR_HUB_APP_NAME": app_name,
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "claude_code_remote.server.main:app",
         "--host", "127.0.0.1", "--port", str(app_port),
         "--log-level", "warning"],
        env=app_env, cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    _wait_url(f"http://127.0.0.1:{app_port}/healthz", timeout=15)
    return proc


@pytest.fixture
def multi_app_hub(tmp_path):
    """M-Hub-5: hub + 2 个 apps. function scope (每个测试拿全新环境)."""
    hub_db = tmp_path / "hub.db"
    hub_port = _free_port()
    admin_email = "test@example.com"
    admin_pw = "test-password"

    hub_env_vars = os.environ.copy()
    hub_env_vars.update({
        "CCR_HUB_DB": str(hub_db),
        "CCR_HUB_ADMIN_EMAIL": admin_email,
        "CCR_HUB_ADMIN_PW": admin_pw,
    })
    hub_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "claude_code_remote.hub.main:app",
         "--host", "127.0.0.1", "--port", str(hub_port),
         "--log-level", "warning"],
        env=hub_env_vars, cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    hub_url = f"http://127.0.0.1:{hub_port}"
    try:
        _wait_url(f"{hub_url}/healthz", timeout=15)
    except Exception:
        hub_proc.terminate()
        out = hub_proc.stdout.read().decode(errors="replace") if hub_proc.stdout else ""
        raise RuntimeError(f"hub failed: {out}")

    # Login + redeem 2 apps
    with httpx.Client(base_url=hub_url, timeout=5) as c:
        c.post("/api/hub/login", json={
            "email": admin_email, "password": admin_pw,
        }).raise_for_status()
        pair1 = c.post("/api/hub/pair").json()
        red1 = c.post("/api/hub/pair/redeem", json={
            "code": pair1["code"], "app_name": "app-A",
        }).json()
        pair2 = c.post("/api/hub/pair").json()
        red2 = c.post("/api/hub/pair/redeem", json={
            "code": pair2["code"], "app_name": "app-B",
        }).json()

    cwd_a = tmp_path / "cwd-a"; cwd_a.mkdir()
    cwd_b = tmp_path / "cwd-b"; cwd_b.mkdir()
    port_a = _free_port()
    port_b = _free_port()
    a_proc = _spawn_app(hub_url, red1["device_token"], "app-A",
                        str(tmp_path / "app-a.sqlite"), str(cwd_a), port_a)
    b_proc = _spawn_app(hub_url, red2["device_token"], "app-B",
                        str(tmp_path / "app-b.sqlite"), str(cwd_b), port_b)

    # 等两个都 online
    _wait_app_online(hub_url, admin_email, admin_pw, timeout=15)
    # 再等第二个
    with httpx.Client(base_url=hub_url, timeout=5) as c:
        c.post("/api/hub/login", json={
            "email": admin_email, "password": admin_pw,
        }).raise_for_status()
        deadline = time.time() + 10
        while time.time() < deadline:
            apps = c.get("/api/hub/apps").json()
            online = [a for a in apps if a["online"]]
            if len(online) >= 2:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"both apps not online: {apps}")

    yield {
        "hub_url": hub_url,
        "ws_hub_url": hub_url.replace("http://", "ws://"),
        "admin_email": admin_email,
        "admin_pw": admin_pw,
        "app_a": {"id": red1["app_id"], "cwd": str(cwd_a),
                   "port": port_a, "proc": a_proc},
        "app_b": {"id": red2["app_id"], "cwd": str(cwd_b),
                   "port": port_b, "proc": b_proc},
    }

    for p in (a_proc, b_proc, hub_proc):
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
