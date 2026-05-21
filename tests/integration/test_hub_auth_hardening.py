"""Hub auth 硬化:
- 密码 hash 改 PBKDF2-HMAC-SHA256 + salt + 600k iterations (零依赖, OWASP 2023)
- session 持久化到 db (重启 hub 后 cookie 不失效)
- 老 sha256 hash 自动升级
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p


def _wait_url(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"server not ready: {url}")


def _spawn_hub(db_path: str, port: int, admin_email: str, admin_pw: str):
    env = os.environ.copy()
    env.update({
        "CCR_HUB_DB": db_path,
        "CCR_HUB_ADMIN_EMAIL": admin_email,
        "CCR_HUB_ADMIN_PW": admin_pw,
    })
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "claude_code_remote.hub.main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        env=env, cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def test_password_hash_uses_pbkdf2(tmp_path):
    """启动 hub → ensure_admin 写入 db → password_hash 字段必须是 pbkdf2 格式."""
    db = tmp_path / "auth-pbkdf2.db"
    port = _free_port()
    proc = _spawn_hub(str(db), port, "u@x.com", "secret-pw")
    try:
        _wait_url(f"http://127.0.0.1:{port}/healthz")
        # hub 用 WAL, 读一下 password_hash 字段
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT password_hash FROM users WHERE email=?", ("u@x.com",),
        ).fetchone()
        conn.close()
        assert row is not None
        h = row["password_hash"]
        assert h.startswith("pbkdf2_sha256$"), f"non-pbkdf2 hash: {h!r}"
        parts = h.split("$")
        assert len(parts) == 4, parts
        iters = int(parts[1])
        assert iters >= 100_000, f"iterations too low: {iters}"
    finally:
        proc.terminate(); proc.wait(timeout=3)


def test_password_verify_correct_and_wrong(tmp_path):
    db = tmp_path / "auth-verify.db"
    port = _free_port()
    proc = _spawn_hub(str(db), port, "u@x.com", "right-pw")
    try:
        _wait_url(f"http://127.0.0.1:{port}/healthz")
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=5) as c:
            r = c.post("/api/hub/login", json={
                "email": "u@x.com", "password": "right-pw",
            })
            assert r.status_code == 200, r.text
            r = c.post("/api/hub/login", json={
                "email": "u@x.com", "password": "wrong-pw",
            })
            assert r.status_code == 401, r.text
    finally:
        proc.terminate(); proc.wait(timeout=3)


def test_session_persists_across_hub_restart(tmp_path):
    """登录 → kill hub → restart → 带原 cookie 调 /api/me 仍认得."""
    db = tmp_path / "auth-persist.db"
    port = _free_port()
    proc = _spawn_hub(str(db), port, "u@x.com", "pw")
    cookie = None
    try:
        _wait_url(f"http://127.0.0.1:{port}/healthz")
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=5) as c:
            c.post("/api/hub/login", json={
                "email": "u@x.com", "password": "pw",
            }).raise_for_status()
            cookie = c.cookies.get("ccr_sess")
            assert cookie
        proc.terminate(); proc.wait(timeout=3)
    finally:
        try: proc.terminate(); proc.wait(timeout=1)
        except Exception: pass

    # restart on same db + same port (port 可能被 OS hold 几秒, 拿新 port)
    port2 = _free_port()
    proc2 = _spawn_hub(str(db), port2, "u@x.com", "pw")
    try:
        _wait_url(f"http://127.0.0.1:{port2}/healthz")
        r = httpx.get(
            f"http://127.0.0.1:{port2}/api/me",
            cookies={"ccr_sess": cookie}, timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "hub"
        assert body["user_id"] is not None, (
            "session 不持久化, 重启 hub 后 cookie 失效"
        )
    finally:
        proc2.terminate(); proc2.wait(timeout=3)


def test_legacy_sha256_password_auto_upgrades(tmp_path):
    """db 里已有 老 sha256 hash 时, verify 通过, hash 自动升级成 pbkdf2."""
    import hashlib
    db = tmp_path / "auth-legacy.db"

    # 手动初始化一个 users 表 + 写老 sha256 hash
    conn = sqlite3.connect(str(db))
    conn.execute("""
      CREATE TABLE users (
        id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, created_at REAL NOT NULL)""")
    legacy_hash = hashlib.sha256(b"legacy-pw").hexdigest()
    conn.execute(
        "INSERT INTO users(id, email, password_hash, created_at) "
        "VALUES('user-leg','u@x.com',?,?)",
        (legacy_hash, time.time()),
    )
    conn.commit(); conn.close()

    port = _free_port()
    # 启动时把 admin_pw 设成 legacy-pw, 但 ensure_admin 看到已有 user, 不会覆盖.
    proc = _spawn_hub(str(db), port, "u@x.com", "legacy-pw")
    try:
        _wait_url(f"http://127.0.0.1:{port}/healthz")
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=5) as c:
            r = c.post("/api/hub/login", json={
                "email": "u@x.com", "password": "legacy-pw",
            })
            assert r.status_code == 200, r.text
        # 现在读 db, 该 hash 应已升级成 pbkdf2 格式
        conn = sqlite3.connect(str(db))
        h = conn.execute(
            "SELECT password_hash FROM users WHERE email=?", ("u@x.com",),
        ).fetchone()[0]
        conn.close()
        assert h.startswith("pbkdf2_sha256$"), (
            f"老 sha256 hash 应被自动升级: {h!r}"
        )
    finally:
        proc.terminate(); proc.wait(timeout=3)
