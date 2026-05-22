"""Hub-side SQLite (ccr-hub-spec.html §9). 风格跟 server/db.py 一致 — 内置
sqlite3 + asyncio.to_thread, 零外部依赖.

5 张表: users / apps / pairing_tokens / sessions_cache / sessions (auth).

密码 hash: PBKDF2-HMAC-SHA256 + 16-byte salt + 600k iterations
(OWASP 2023). 旧 sha256 hash 在 verify 通过后自动升级.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

_PBKDF2_ITERS = 600_000
_PBKDF2_SALT_BYTES = 16

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
  id                    TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL,
  name                  TEXT NOT NULL,
  device_token_hash     TEXT NOT NULL,
  last_seen_at          REAL,
  capabilities_json     TEXT NOT NULL DEFAULT '[]',
  revoked_at            REAL,
  total_online_seconds  INTEGER NOT NULL DEFAULT 0,
  created_at            REAL NOT NULL
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
  app_id              TEXT NOT NULL,
  sid                 TEXT NOT NULL,
  user_id             TEXT NOT NULL,
  name                TEXT,
  cwd                 TEXT,
  state               TEXT,
  last_active         REAL,
  model               TEXT,
  effort              TEXT,
  permission_mode     TEXT,
  is_stash            INTEGER NOT NULL DEFAULT 0,
  is_inactive         INTEGER NOT NULL DEFAULT 0,
  pending_permissions INTEGER NOT NULL DEFAULT 0,
  needs_action_detail TEXT,
  created_at          REAL,
  updated_at          REAL,
  PRIMARY KEY (app_id, sid)
);
CREATE INDEX IF NOT EXISTS idx_sessions_cache_user
  ON sessions_cache(user_id, last_active DESC);

-- 用户登录 session (持久化, hub 重启不失效)
CREATE TABLE IF NOT EXISTS auth_sessions (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  created_at  REAL NOT NULL,
  expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_exp ON auth_sessions(expires_at);

-- Passkey (FIDO2 / WebAuthn) 凭据 — 每用户多条 (每个设备一条).
-- public_key 是 COSE-encoded bytes (BLOB). credential_id 用 base64url 当主键
-- (FIDO2 spec 推荐, 唯一性来自 authenticator).
CREATE TABLE IF NOT EXISTS passkeys (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  public_key    BLOB NOT NULL,
  sign_count    INTEGER NOT NULL DEFAULT 0,
  transports    TEXT,
  nickname      TEXT,
  created_at    REAL NOT NULL,
  last_used_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_passkeys_user ON passkeys(user_id);
"""


def hash_password(pw: str) -> str:
    """PBKDF2-HMAC-SHA256, 600k iterations, 16-byte random salt. 格式:
        pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>
    """
    salt = secrets.token_bytes(_PBKDF2_SALT_BYTES)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    """常数时间比较. 支持 pbkdf2 + 老 sha256 (返 True 让上层触发 rehash)."""
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters_s, salt_hex, hash_hex = stored.split("$", 3)
            iters = int(iters_s)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
            got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                       salt, iters)
            return hmac.compare_digest(expected, got)
        except Exception:
            return False
    # 老 sha256(plaintext) 兼容路径
    legacy = hashlib.sha256(pw.encode("utf-8")).hexdigest()
    return hmac.compare_digest(stored, legacy)


def needs_rehash(stored: str) -> bool:
    """老 sha256 / 低 iters → 需要重 hash 升级."""
    if not stored.startswith("pbkdf2_sha256$"):
        return True
    try:
        iters = int(stored.split("$")[1])
        return iters < _PBKDF2_ITERS
    except Exception:
        return True


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
    # Schema migration: 加新字段时老 db 不存在 — ALTER TABLE 兜底.
    # 加新 column 必须每条独立 try/except IGNORE (sqlite 不支持 IF NOT EXISTS
    # for ADD COLUMN).
    def _migrate():
        c = _conn
        for ddl in (
            "ALTER TABLE sessions_cache ADD COLUMN is_stash INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sessions_cache ADD COLUMN is_inactive INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sessions_cache ADD COLUMN pending_permissions INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sessions_cache ADD COLUMN needs_action_detail TEXT",
            "ALTER TABLE apps ADD COLUMN total_online_seconds INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass   # column 已存在, 忽略
    await asyncio.to_thread(_migrate)


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
        # 老库存在; 不覆盖密码 (用户已经改过), 但若仍是老 sha256 等下次 login
        # 触发自动 rehash. 这里只补 schema 缺失数据.
        return row["id"]
    uid = "user-" + secrets.token_hex(8)
    c.execute(
        "INSERT INTO users(id, email, password_hash, created_at) "
        "VALUES(?,?,?,?)",
        (uid, email, pw_hash, time.time()),
    )
    return uid


async def ensure_admin(email: str, password: str) -> str:
    # 注: hash_password 走 PBKDF2 随机 salt, 每次结果不同 — 不能用作 dedup key.
    return await asyncio.to_thread(
        _ensure_admin_sync, email, hash_password(password),
    )


def _verify_login_sync(email: str, password: str) -> str | None:
    c = _get_conn()
    row = c.execute(
        "SELECT id, password_hash FROM users WHERE email=?", (email,),
    ).fetchone()
    if not row:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    # 自动升级老格式 / 低 iters
    if needs_rehash(row["password_hash"]):
        c.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(password), row["id"]),
        )
    return row["id"]


async def verify_login(email: str, password: str) -> str | None:
    return await asyncio.to_thread(_verify_login_sync, email, password)


# ---- auth_sessions (持久化登录 session) ----

async def create_auth_session(user_id: str, ttl_seconds: int) -> str:
    sid = secrets.token_urlsafe(24)
    now = time.time()

    def _q():
        _get_conn().execute(
            "INSERT INTO auth_sessions(id, user_id, created_at, expires_at) "
            "VALUES(?,?,?,?)",
            (sid, user_id, now, now + ttl_seconds),
        )
    await asyncio.to_thread(_q)
    return sid


async def get_auth_session_user(sid: str) -> str | None:
    def _q():
        row = _get_conn().execute(
            "SELECT user_id, expires_at FROM auth_sessions WHERE id=?", (sid,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < time.time():
            return None
        return row["user_id"]
    return await asyncio.to_thread(_q)


async def destroy_auth_session(sid: str) -> None:
    def _q():
        _get_conn().execute("DELETE FROM auth_sessions WHERE id=?", (sid,))
    await asyncio.to_thread(_q)


async def purge_expired_auth_sessions() -> int:
    def _q():
        c = _get_conn().execute(
            "DELETE FROM auth_sessions WHERE expires_at < ?", (time.time(),),
        )
        return c.rowcount
    return await asyncio.to_thread(_q)


# ---- passkeys (WebAuthn / FIDO2 credentials) ----

async def add_passkey(credential_id: str, user_id: str, public_key: bytes,
                      sign_count: int, transports: str | None = None,
                      nickname: str | None = None) -> None:
    def _q():
        _get_conn().execute(
            "INSERT INTO passkeys(id, user_id, public_key, sign_count, "
            "transports, nickname, created_at) VALUES(?,?,?,?,?,?,?)",
            (credential_id, user_id, public_key, sign_count,
             transports, nickname, time.time()),
        )
    await asyncio.to_thread(_q)


async def get_passkey(credential_id: str) -> dict | None:
    def _q():
        row = _get_conn().execute(
            "SELECT * FROM passkeys WHERE id=?", (credential_id,),
        ).fetchone()
        return dict(row) if row else None
    return await asyncio.to_thread(_q)


async def list_passkeys_for_user(user_id: str) -> list[dict]:
    def _q():
        rows = _get_conn().execute(
            "SELECT id, nickname, created_at, last_used_at "
            "FROM passkeys WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_q)


async def bump_passkey_sign_count(credential_id: str, sign_count: int) -> None:
    def _q():
        _get_conn().execute(
            "UPDATE passkeys SET sign_count=?, last_used_at=? WHERE id=?",
            (sign_count, time.time(), credential_id),
        )
    await asyncio.to_thread(_q)


async def delete_passkey(credential_id: str, user_id: str) -> bool:
    """删除该 user 拥有的 passkey. 返 True 当真删了."""
    def _q():
        c = _get_conn().execute(
            "DELETE FROM passkeys WHERE id=? AND user_id=?",
            (credential_id, user_id),
        )
        return c.rowcount > 0
    return await asyncio.to_thread(_q)


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
            "SELECT id, user_id, name, last_seen_at, revoked_at, created_at, "
            "total_online_seconds FROM apps "
            "WHERE user_id=? AND revoked_at IS NULL "
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


async def bump_app_online_time(app_id: str, seconds: float) -> None:
    """app 反向 WS 断开时调, 累加这次在线时长."""
    if seconds <= 0:
        return
    def _q():
        _get_conn().execute(
            "UPDATE apps SET total_online_seconds = total_online_seconds + ? "
            "WHERE id=?",
            (int(seconds), app_id),
        )
    await asyncio.to_thread(_q)


async def revoke_app(app_id: str) -> bool:
    def _q():
        c = _get_conn()
        cur = c.execute(
            "UPDATE apps SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (time.time(), app_id),
        )
        return cur.rowcount > 0
    return await asyncio.to_thread(_q)


def _create_app_sync(user_id: str, name: str, tok_hash: str) -> str:
    c = _get_conn()
    app_id = "app-" + secrets.token_hex(8)
    c.execute(
        "INSERT INTO apps(id, user_id, name, device_token_hash, created_at) "
        "VALUES(?,?,?,?,?)",
        (app_id, user_id, name, tok_hash, time.time()),
    )
    return app_id


async def create_app(user_id: str, name: str, device_token: str) -> str:
    return await asyncio.to_thread(
        _create_app_sync, user_id, name, hash_token(device_token),
    )


# ---- pairing tokens ----

def _make_pair_code() -> str:
    """6 位数字 + 8 字符 hex (不冲突). e.g. "423917-a1b2c3d4"."""
    return f"{secrets.randbelow(900_000) + 100_000:06d}-{secrets.token_hex(4)}"


async def create_pairing(user_id: str, ttl_seconds: int = 300) -> dict:
    def _q():
        c = _get_conn()
        code = _make_pair_code()
        exp = time.time() + ttl_seconds
        c.execute(
            "INSERT INTO pairing_tokens(code, user_id, expires_at) "
            "VALUES(?,?,?)",
            (code, user_id, exp),
        )
        return {"code": code, "expires_at": exp}
    return await asyncio.to_thread(_q)


def _consume_pairing_sync(code: str) -> str | None:
    """返回 user_id, 或 None 当 code 无效 / 过期 / 已消费. 一次性消费."""
    c = _get_conn()
    row = c.execute(
        "SELECT user_id, expires_at, consumed_at FROM pairing_tokens "
        "WHERE code=?",
        (code,),
    ).fetchone()
    if not row:
        return None
    if row["consumed_at"] is not None:
        return None
    if row["expires_at"] < time.time():
        return None
    c.execute(
        "UPDATE pairing_tokens SET consumed_at=? WHERE code=?",
        (time.time(), code),
    )
    return row["user_id"]


async def consume_pairing(code: str) -> str | None:
    return await asyncio.to_thread(_consume_pairing_sync, code)


# ---- sessions_cache ----

_CACHE_COLS = (
    "name", "cwd", "state", "last_active", "model", "effort",
    "permission_mode", "is_stash", "is_inactive", "pending_permissions",
    "needs_action_detail", "created_at",
)


def _upsert_session_sync(app_id: str, user_id: str, sid: str,
                         fields: dict) -> None:
    """UPSERT 一条 sessions_cache. fields 缺的 key 不动 (partial update)."""
    c = _get_conn()
    cur = c.execute(
        "SELECT 1 FROM sessions_cache WHERE app_id=? AND sid=?",
        (app_id, sid),
    ).fetchone()
    now = time.time()
    if cur:
        # UPDATE — 只动 fields 有的列
        kept = {k: v for k, v in fields.items() if k in _CACHE_COLS}
        if kept:
            cols = ", ".join(f"{k}=?" for k in kept)
            vals = list(kept.values())
            c.execute(
                f"UPDATE sessions_cache SET {cols}, updated_at=? "
                "WHERE app_id=? AND sid=?",
                (*vals, now, app_id, sid),
            )
        else:
            c.execute(
                "UPDATE sessions_cache SET updated_at=? "
                "WHERE app_id=? AND sid=?",
                (now, app_id, sid),
            )
    else:
        # INSERT — 缺字段填默认
        c.execute(
            "INSERT INTO sessions_cache(app_id, sid, user_id, name, cwd, "
            "state, last_active, model, effort, permission_mode, "
            "is_stash, is_inactive, pending_permissions, needs_action_detail, "
            "created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                app_id, sid, user_id,
                fields.get("name", ""),
                fields.get("cwd", ""),
                fields.get("state"),
                fields.get("last_active"),
                fields.get("model", ""),
                fields.get("effort", ""),
                fields.get("permission_mode"),
                int(bool(fields.get("is_stash"))),
                int(bool(fields.get("is_inactive"))),
                int(fields.get("pending_permissions") or 0),
                fields.get("needs_action_detail"),
                fields.get("created_at", now),
                now,
            ),
        )


async def upsert_session(app_id: str, user_id: str, sid: str,
                         fields: dict) -> None:
    await asyncio.to_thread(_upsert_session_sync, app_id, user_id, sid, fields)


async def remove_session(app_id: str, sid: str) -> None:
    def _q():
        _get_conn().execute(
            "DELETE FROM sessions_cache WHERE app_id=? AND sid=?",
            (app_id, sid),
        )
    await asyncio.to_thread(_q)


async def clear_app_sessions(app_id: str) -> None:
    """app 发 sessions_snapshot 前先清自己的 cache, 避免老条目残留."""
    def _q():
        _get_conn().execute(
            "DELETE FROM sessions_cache WHERE app_id=?", (app_id,),
        )
    await asyncio.to_thread(_q)


async def list_user_sessions(user_id: str) -> list[dict]:
    """该 user 所有 apps 的合并 sessions, 按 last_active desc."""
    def _q():
        rows = _get_conn().execute(
            "SELECT sc.*, a.name AS app_name "
            "FROM sessions_cache sc "
            "JOIN apps a ON a.id = sc.app_id "
            "WHERE sc.user_id=? "
            "ORDER BY COALESCE(sc.last_active, sc.updated_at) DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_q)
