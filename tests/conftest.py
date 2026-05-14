"""Test fixtures: boot a real CCR server with isolated DB + fake Claude.

Layout:
    server_env       (session)  empty DB; everything fresh
    live_server_env  (session)  snapshot of user's real DB (see seed_from_live.py)
    base_url         (session)  alias from server_env
    test_token       (session)  bearer token in use
    page             (function) pytest-playwright; navigates to fresh tab
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = PROJECT_ROOT / "tests" / "fakes" / "fake_claude.py"
LIVE_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "live"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthz(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as e:
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(f"server didn't pass healthz at {url}: {last_err}")


def _start_server(*, db_path: str, default_cwd: str,
                  home_override: str | None = None):
    """Start uvicorn with given DB + cwd. Generator: yields env dict, cleans up."""
    token = secrets.token_hex(16)
    port = _free_port()

    # Scrub the user's CCR_* (esp. CCR_ROOT_PATH, CCR_TOKEN from a running prod
    # install) so the test server sees only our explicit values
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("CCR_")}
    env = {
        **base_env,
        "CCR_TOKEN": token,
        "CCR_HOST": "127.0.0.1",
        "CCR_PORT": str(port),
        "CCR_DB_PATH": db_path,
        "CCR_DEFAULT_CWD": default_cwd,
        "CCR_CLAUDE_BIN": str(FAKE_CLAUDE),
        "CCR_ROOT_PATH": "",
        "PYTHONUNBUFFERED": "1",
    }
    if home_override:
        env["HOME"] = home_override

    log_path = tempfile.mktemp(prefix="ccr-test-server-", suffix=".log")
    log_fh = open(log_path, "w")

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "claude_code_remote.server.main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        env=env,
        cwd=str(PROJECT_ROOT),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_healthz(f"{base_url}/healthz")
    except Exception:
        proc.terminate()
        proc.wait(timeout=5)
        log_fh.close()
        sys.stderr.write(f"\n--- server log ({log_path}) ---\n")
        sys.stderr.write(Path(log_path).read_text())
        raise

    try:
        yield {
            "base_url": base_url,
            "token": token,
            "db_path": db_path,
            "default_cwd": default_cwd,
            "home": home_override,
            "log_path": log_path,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        log_fh.close()


@pytest.fixture(scope="session")
def server_env():
    db_path = tempfile.mktemp(prefix="ccr-test-", suffix=".sqlite")
    default_cwd = tempfile.mkdtemp(prefix="ccr-test-cwd-")
    try:
        yield from _start_server(db_path=db_path, default_cwd=default_cwd)
    finally:
        # best-effort cleanup
        for p in (db_path, default_cwd):
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass


@pytest.fixture(scope="session")
def live_server_env():
    """Server backed by a copy of the user's real CCR DB.

    Snapshot is created once via `python3 tests/seed_from_live.py`. Each test
    session copies that snapshot to /tmp so mutations don't touch the fixture.

    Also sets HOME to a tmp dir whose .claude/projects symlinks to the snapshot
    so /api/sessions/<id>/ctx (which reads claude jsonl files) works.
    """
    src_db = LIVE_FIXTURE_DIR / "ccr.sqlite"
    if not src_db.exists():
        pytest.skip(f"no live snapshot at {LIVE_FIXTURE_DIR} — "
                    "run `python3 tests/seed_from_live.py` first")

    # 1. copy DB into tmp so tests can mutate (delete, etc.)
    db_path = tempfile.mktemp(prefix="ccr-test-live-", suffix=".sqlite")
    shutil.copy(src_db, db_path)

    # 2. fake HOME that points .claude/projects at our snapshot
    fake_home = tempfile.mkdtemp(prefix="ccr-test-home-")
    snap_projects = LIVE_FIXTURE_DIR / "claude" / "projects"
    fake_claude_dir = Path(fake_home) / ".claude"
    fake_claude_dir.mkdir(parents=True, exist_ok=True)
    if snap_projects.exists():
        (fake_claude_dir / "projects").symlink_to(snap_projects)

    # 3. default cwd — point at a real path so directory ops don't 404
    default_cwd = tempfile.mkdtemp(prefix="ccr-test-cwd-")

    try:
        yield from _start_server(db_path=db_path, default_cwd=default_cwd,
                                  home_override=fake_home)
    finally:
        for p in (db_path, default_cwd, fake_home):
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass


@pytest.fixture
def spawned_session(server_env, tmp_path):
    """Spawn-via-API helper that auto-cleans on test exit.

    Yields a callable:  sid = spawned_session(name="...", cwd=None)
    """
    from tests.helpers import api_spawn, api_delete_session
    created: list[str] = []
    base_url = server_env["base_url"]
    token = server_env["token"]

    def _spawn(name: str = "test-session", cwd: str | None = None) -> str:
        cwd = cwd or str(tmp_path)
        sid = api_spawn(base_url, token, cwd, name)
        created.append(sid)
        return sid

    yield _spawn

    for sid in created:
        try:
            api_delete_session(base_url, token, sid)
        except Exception:
            pass


@pytest.fixture(scope="session")
def live_db():
    """Read-only access to the snapshot DB for tests that want to assert
    "what's in the DB" without going through the API. Don't write."""
    import sqlite3
    src = LIVE_FIXTURE_DIR / "ccr.sqlite"
    if not src.exists():
        pytest.skip("no live snapshot")
    conn = sqlite3.connect(src)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def base_url(server_env):
    return server_env["base_url"]


@pytest.fixture(scope="session")
def test_token(server_env):
    return server_env["token"]


@pytest.fixture
def fresh_page(page, base_url):
    """Page with localStorage cleared before any app code runs."""
    page.add_init_script("try { localStorage.clear(); } catch (e) {}")
    page.goto(base_url)
    return page


@pytest.fixture
def logged_in_page(page, base_url, test_token):
    """Pre-authenticated page — drops you in #view-home."""
    page.add_init_script(
        "try { localStorage.setItem('ccr.token', " + repr(test_token) + "); } catch (e) {}"
    )
    page.goto(base_url)
    return page
