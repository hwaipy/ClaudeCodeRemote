"""Hub-side SQLite (ccr-hub-spec.html §9). 风格跟 server/db.py 一致 — 内置
sqlite3 + asyncio.to_thread, 零外部依赖.

4 张表: users / apps / pairing_tokens / sessions_cache.
M-Hub-0 只用 users + apps; 后续逐步加 pairing + cache.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

_DB_PATH: Path | None = None
_lock = asyncio.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS apps (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  name              TEXT NOT NULL,
  device_token_hash TEXT NOT NULL,
  last_seen_at      REAL,
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  revoked_at        REAL,
  created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_apps_user  ON apps(user_id);
CREATE INDEX IF NOT EXISTS idx_apps_token ON apps(device_token_hash);

CREATE TABLE IF NOT EXISTS pairing_tokens (
  code        TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  expires_at  REAL NOT NULL,
  consumed_at REAL
);

CREATE TABLE IF NOT EXISTS sessions_cache (
  app_id          TEXT NOT NULL,
  sid             TEXT NOT NULL,
  user_id         TEXT NOT NULL,
  name            TEXT,
  cwd             TEXT,
  state           TEXT,
  last_active     REAL,
  model           TEXT,
  effort          TEXT,
  permission_mode TEXT,
  created_at      REAL,
  updated_at      REAL,
  PRIMARY KEY (app_id, sid)
);
CREATE INDEX IF NOT EXISTS idx_sessions_cache_user
  ON sessions_cache(user_id, last_active DESC);
"""


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


# ---- Connection ----

def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    assert _conn is not None, "hub db not initialized"
    return _conn


async def init(path: str | Path) -> None:
    global _DB_PATH, _conn
    _DB_PATH = Path(path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = await asyncio.to_thread(_connect, _DB_PATH)
    await asyncio.to_thread(_conn.executescript, _SCHEMA)


async def close() -> None:
    global _conn
    if _conn is not None:
        c, _conn = _conn, None
        await asyncio.to_thread(c.close)


# ---- users ----

def _ensure_admin_sync(email: str, pw_hash: str) -> str:
    c = _get_conn()
    row = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if row:
        return row["id"]
    uid = "user-" + secrets.token_hex(8)
    c.execute(
        "INSERT INTO users(id, email, password_hash, created_at) "
        "VALUES(?,?,?,?)",
        (uid, email, pw_hash, time.time()),
    )
    return uid


async def ensure_admin(email: str, password: str) -> str:
    return await asyncio.to_thread(
        _ensure_admin_sync, email, hash_password(password),
    )


def _verify_login_sync(email: str, pw_hash: str) -> str | None:
    row = _get_conn().execute(
        "SELECT id FROM users WHERE email=? AND password_hash=?",
        (email, pw_hash),
    ).fetchone()
    return row["id"] if row else None


async def verify_login(email: str, password: str) -> str | None:
    return await asyncio.to_thread(
        _verify_login_sync, email, hash_password(password),
    )


async def get_user_by_id(user_id: str) -> dict | None:
    def _q():
        row = _get_conn().execute(
            "SELECT id, email FROM users WHERE id=?", (user_id,),
        ).fetchone()
        return dict(row) if row else None
    return await asyncio.to_thread(_q)


# ---- apps ----

def _ensure_seed_app_sync(
    admin_email: str, name: str, tok_hash: str,
) -> str:
    c = _get_conn()
    u = c.execute(
        "SELECT id FROM users WHERE email=?", (admin_email,),
    ).fetchone()
    if not u:
        raise RuntimeError(f"admin user {admin_email!r} not found for seed")
    user_id = u["id"]
    row = c.execute(
        "SELECT id FROM apps WHERE user_id=? AND name=?", (user_id, name),
    ).fetchone()
    if row:
        c.execute(
            "UPDATE apps SET device_token_hash=?, revoked_at=NULL WHERE id=?",
            (tok_hash, row["id"]),
        )
        return row["id"]
    app_id = "app-" + secrets.token_hex(8)
    c.execute(
        "INSERT INTO apps(id, user_id, name, device_token_hash, created_at) "
        "VALUES(?,?,?,?,?)",
        (app_id, user_id, name, tok_hash, time.time()),
    )
    return app_id


async def ensure_seed_app(
    admin_email: str, name: str, device_token: str,
) -> str:
    return await asyncio.to_thread(
        _ensure_seed_app_sync, admin_email, name, hash_token(device_token),
    )


async def find_app_by_token(token: str) -> dict | None:
    def _q():
        tok_hash = hash_token(token)
        row = _get_conn().execute(
            "SELECT * FROM apps WHERE device_token_hash=? AND revoked_at IS NULL",
            (tok_hash,),
        ).fetchone()
        return dict(row) if row else None
    return await asyncio.to_thread(_q)


async def list_apps_for_user(user_id: str) -> list[dict]:
    def _q():
        rows = _get_conn().execute(
            "SELECT id, user_id, name, last_seen_at, revoked_at, created_at "
            "FROM apps WHERE user_id=? AND revoked_at IS NULL "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_q)


async def touch_app_seen(app_id: str) -> None:
    def _q():
        _get_conn().execute(
            "UPDATE apps SET last_seen_at=? WHERE id=?",
            (time.time(), app_id),
        )
    await asyncio.to_thread(_q)
