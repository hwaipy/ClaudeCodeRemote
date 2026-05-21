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
