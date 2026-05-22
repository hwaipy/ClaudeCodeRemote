"""Passkey endpoint shape + auth gating.

不模拟真 authenticator (要做 ed25519 软实现 + COSE 编码, 重). 只验:
- /passkey/register/start 需登录, 返 CreationOptions JSON
- /passkey/login/start 公开, 返 RequestOptions JSON
- /passkeys list/delete 需登录
- 老 sha256 密码 + passkey 同时存在不冲突
"""
from __future__ import annotations

import httpx


def _login(hub_url, email, pw):
    """返一个已登录的 httpx.Client. 调用方负责 .close() 或用 with-as."""
    c = httpx.Client(base_url=hub_url, timeout=5)
    c.post("/api/hub/login", json={"email": email, "password": pw}).raise_for_status()
    return c   # 已 lazy-enter; 调用方直接调 c.post / c.close().


def test_register_start_requires_login(hub_env):
    """匿名 register/start → 401."""
    r = httpx.post(
        hub_env["base_url"] + "/api/hub/passkey/register/start",
        json={}, timeout=5,
    )
    assert r.status_code == 401, r.text


def test_register_start_returns_creation_options(hub_env):
    """登录后 → 返 PublicKeyCredentialCreationOptions, 含 challenge / rp / user."""
    c = _login(hub_env["base_url"], hub_env["admin_email"], hub_env["admin_pw"])
    try:
        r = c.post("/api/hub/passkey/register/start", json={"nickname": "iPhone"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "challenge" in body
        assert isinstance(body["challenge"], str) and len(body["challenge"]) > 8
        assert body["rp"]["id"]   # RP_ID
        assert body["user"]["id"]
    finally:
        c.close()


def test_login_start_is_public(hub_env):
    """匿名 login/start → 返 PublicKeyCredentialRequestOptions."""
    r = httpx.post(hub_env["base_url"] + "/api/hub/passkey/login/start",
                   timeout=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "challenge" in body
    assert "rpId" in body


def test_login_finish_rejects_bad_credential(hub_env):
    """瞎构造的 credential → 400/403."""
    r = httpx.post(
        hub_env["base_url"] + "/api/hub/passkey/login/finish",
        json={"credential": {"id": "bogus", "response": {"clientDataJSON": "x"}}},
        timeout=5,
    )
    assert r.status_code in (400, 403), r.text


def test_passkeys_list_requires_login(hub_env):
    r = httpx.get(hub_env["base_url"] + "/api/hub/passkeys", timeout=5)
    assert r.status_code in (401, 403), r.text


def test_passkeys_list_empty_for_new_user(hub_env):
    c = _login(hub_env["base_url"], hub_env["admin_email"], hub_env["admin_pw"])
    try:
        r = c.get("/api/hub/passkeys")
        assert r.status_code == 200
        assert r.json() == []
    finally:
        c.close()


def test_delete_unknown_passkey_404(hub_env):
    c = _login(hub_env["base_url"], hub_env["admin_email"], hub_env["admin_pw"])
    try:
        r = c.delete("/api/hub/passkeys/nonexistent-credential-id")
        assert r.status_code == 404, r.text
    finally:
        c.close()


def test_db_passkeys_table_persists_across_restart(tmp_path):
    """加一条 passkey, restart hub, 还在."""
    import os, subprocess, sys, socket, time
    db = tmp_path / "pk-persist.db"

    def _free_port():
        s = socket.socket(); s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]; s.close(); return p

    def _wait(url, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(url, timeout=1).status_code < 500: return
            except: pass
            time.sleep(0.1)
        raise RuntimeError(f"not ready: {url}")

    PROJECT_ROOT = "/home/hwaipy/codes/ClaudeCodeRemoteAutoTest/ClaudeCodeRemote"
    env = os.environ.copy()
    env.update({
        "CCR_HUB_DB": str(db),
        "CCR_HUB_ADMIN_EMAIL": "u@x.com",
        "CCR_HUB_ADMIN_PW": "pw",
    })
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "claude_code_remote.hub.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait(f"http://127.0.0.1:{port}/healthz")
        # 用 sqlite3 直接 insert 一条 passkey (模拟 register 完成)
        import sqlite3, time as _t
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO passkeys(id, user_id, public_key, sign_count, "
                     "created_at) VALUES('cred-test', "
                     "(SELECT id FROM users WHERE email='u@x.com'), "
                     "?, 0, ?)", (b"fakepubkey", _t.time()))
        conn.commit(); conn.close()
    finally:
        proc.terminate(); proc.wait(timeout=3)

    # restart on same db, 拉 list 应该返一条
    port2 = _free_port()
    proc2 = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "claude_code_remote.hub.main:app",
         "--host", "127.0.0.1", "--port", str(port2), "--log-level", "warning"],
        env=env, cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait(f"http://127.0.0.1:{port2}/healthz")
        with httpx.Client(base_url=f"http://127.0.0.1:{port2}", timeout=5) as c:
            c.post("/api/hub/login", json={
                "email": "u@x.com", "password": "pw",
            }).raise_for_status()
            r = c.get("/api/hub/passkeys").json()
            assert len(r) == 1
            assert r[0]["id"] == "cred-test"
    finally:
        proc2.terminate(); proc2.wait(timeout=3)
