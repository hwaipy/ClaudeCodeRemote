"""SQLite 持久层。用内置 sqlite3 + asyncio.to_thread 包装，零依赖。

表：
  sessions      会话元数据 + hibernate/finished/deleted 时间戳
  messages      持久事件流（kind ∈ {user, assistant, system_init, result, perm_req, perm_dec, ccr}）
  permissions   会话级白名单（scope ∈ {tool, command}）
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(
    __import__("os").environ.get("CCR_DB_PATH",
                                  str(Path.home() / ".local/share/ccr/ccr.sqlite"))
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    claude_session_id   TEXT,
    name                TEXT NOT NULL DEFAULT '',
    cwd                 TEXT NOT NULL,
    created_at          REAL NOT NULL,
    last_activity_at    REAL NOT NULL,
    hibernated_at       REAL,
    finished_at         REAL,
    deleted_at          REAL,
    deactivated_at      REAL
);

CREATE TABLE IF NOT EXISTS messages (
    sess_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (sess_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_sess_seq ON messages(sess_id, seq);

CREATE TABLE IF NOT EXISTS permissions (
    sess_id     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    key         TEXT NOT NULL,
    ts          REAL NOT NULL,
    PRIMARY KEY (sess_id, scope, key)
);
"""

_conn: sqlite3.Connection | None = None
_lock = asyncio.Lock()


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    # Lightweight migrations for older DBs
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "deactivated_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN deactivated_at REAL")
    return conn


async def init() -> None:
    global _conn
    if _conn is None:
        _conn = await asyncio.to_thread(_open)


async def close() -> None:
    global _conn
    if _conn is not None:
        c = _conn
        _conn = None
        await asyncio.to_thread(c.close)


async def _run(fn, *args, **kwargs):
    async with _lock:
        return await asyncio.to_thread(fn, *args, **kwargs)


# ---------- sessions ----------

async def insert_session(sess_id: str, name: str, cwd: str, created_at: float) -> None:
    def _w() -> None:
        _conn.execute(
            "INSERT INTO sessions(id, name, cwd, created_at, last_activity_at) "
            "VALUES (?,?,?,?,?)",
            (sess_id, name, cwd, created_at, created_at),
        )
    await _run(_w)


async def update_claude_sid(sess_id: str, claude_session_id: str) -> None:
    def _w() -> None:
        _conn.execute(
            "UPDATE sessions SET claude_session_id=? WHERE id=?",
            (claude_session_id, sess_id),
        )
    await _run(_w)


async def update_activity(sess_id: str, ts: float | None = None) -> None:
    ts = ts if ts is not None else time.time()
    def _w() -> None:
        _conn.execute("UPDATE sessions SET last_activity_at=? WHERE id=?",
                       (ts, sess_id))
    await _run(_w)


async def mark_hibernated(sess_id: str) -> None:
    ts = time.time()
    def _w() -> None:
        _conn.execute("UPDATE sessions SET hibernated_at=? WHERE id=?",
                       (ts, sess_id))
    await _run(_w)


async def mark_resumed(sess_id: str) -> None:
    def _w() -> None:
        _conn.execute(
            "UPDATE sessions SET hibernated_at=NULL, last_activity_at=? WHERE id=?",
            (time.time(), sess_id),
        )
    await _run(_w)


async def mark_finished(sess_id: str) -> None:
    def _w() -> None:
        _conn.execute("UPDATE sessions SET finished_at=? WHERE id=?",
                       (time.time(), sess_id))
    await _run(_w)


async def mark_deleted(sess_id: str) -> None:
    def _w() -> None:
        _conn.execute("UPDATE sessions SET deleted_at=? WHERE id=?",
                       (time.time(), sess_id))
    await _run(_w)


async def update_name(sess_id: str, name: str) -> None:
    def _w() -> None:
        _conn.execute("UPDATE sessions SET name=? WHERE id=?", (name, sess_id))
    await _run(_w)


async def mark_deactivated(sess_id: str) -> None:
    def _w() -> None:
        _conn.execute("UPDATE sessions SET deactivated_at=? WHERE id=?",
                       (time.time(), sess_id))
    await _run(_w)


async def mark_activated(sess_id: str) -> None:
    def _w() -> None:
        _conn.execute("UPDATE sessions SET deactivated_at=NULL WHERE id=?",
                       (sess_id,))
    await _run(_w)


_SESS_COLS = ("id", "claude_session_id", "name", "cwd", "created_at",
              "last_activity_at", "hibernated_at", "finished_at", "deleted_at",
              "deactivated_at")


async def get_session(sess_id: str) -> dict[str, Any] | None:
    def _w() -> dict[str, Any] | None:
        cur = _conn.execute(
            f"SELECT {','.join(_SESS_COLS)} FROM sessions WHERE id=?", (sess_id,),
        )
        row = cur.fetchone()
        return dict(zip(_SESS_COLS, row)) if row else None
    return await _run(_w)


async def list_sessions(include_deleted: bool = False) -> list[dict[str, Any]]:
    def _w() -> list[dict[str, Any]]:
        sql = f"SELECT {','.join(_SESS_COLS)} FROM sessions"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY created_at DESC"
        cur = _conn.execute(sql)
        return [dict(zip(_SESS_COLS, r)) for r in cur.fetchall()]
    return await _run(_w)


# ---------- messages ----------

async def append_message(sess_id: str, seq: int, ts: float, kind: str,
                          payload: dict[str, Any]) -> None:
    def _w() -> None:
        _conn.execute(
            "INSERT OR IGNORE INTO messages(sess_id, seq, ts, kind, payload) "
            "VALUES (?,?,?,?,?)",
            (sess_id, seq, ts, kind, json.dumps(payload, ensure_ascii=False)),
        )
    await _run(_w)


async def load_messages(sess_id: str) -> list[tuple[int, float, str, dict[str, Any]]]:
    def _w() -> list[tuple[int, float, str, dict[str, Any]]]:
        cur = _conn.execute(
            "SELECT seq, ts, kind, payload FROM messages WHERE sess_id=? ORDER BY seq",
            (sess_id,),
        )
        return [(r[0], r[1], r[2], json.loads(r[3])) for r in cur.fetchall()]
    return await _run(_w)


async def load_messages_tail(sess_id: str, limit: int) -> list[tuple[int, float, str, dict[str, Any]]]:
    """最后 limit 条，按 seq 升序返回。"""
    def _w() -> list[tuple[int, float, str, dict[str, Any]]]:
        cur = _conn.execute(
            "SELECT seq, ts, kind, payload FROM messages WHERE sess_id=? "
            "ORDER BY seq DESC LIMIT ?",
            (sess_id, limit),
        )
        rows = list(cur.fetchall())
        rows.reverse()
        return [(r[0], r[1], r[2], json.loads(r[3])) for r in rows]
    return await _run(_w)


async def load_messages_before(sess_id: str, before_seq: int, limit: int) -> list[tuple[int, float, str, dict[str, Any]]]:
    """seq < before_seq 的最后 limit 条，按 seq 升序返回（用于向前翻页）。"""
    def _w() -> list[tuple[int, float, str, dict[str, Any]]]:
        cur = _conn.execute(
            "SELECT seq, ts, kind, payload FROM messages WHERE sess_id=? AND seq < ? "
            "ORDER BY seq DESC LIMIT ?",
            (sess_id, before_seq, limit),
        )
        rows = list(cur.fetchall())
        rows.reverse()
        return [(r[0], r[1], r[2], json.loads(r[3])) for r in rows]
    return await _run(_w)


async def load_messages_from(sess_id: str, from_seq: int) -> list[tuple[int, float, str, dict[str, Any]]]:
    """seq >= from_seq 的全部消息，按 seq 升序返回。"""
    def _w() -> list[tuple[int, float, str, dict[str, Any]]]:
        cur = _conn.execute(
            "SELECT seq, ts, kind, payload FROM messages WHERE sess_id=? AND seq >= ? "
            "ORDER BY seq",
            (sess_id, from_seq),
        )
        return [(r[0], r[1], r[2], json.loads(r[3])) for r in cur.fetchall()]
    return await _run(_w)


async def find_last_user_seq(sess_id: str) -> int | None:
    """最后一条 user_input 的 seq；用于"最近一次问答"边界。"""
    def _w() -> int | None:
        cur = _conn.execute(
            "SELECT seq FROM messages WHERE sess_id=? AND kind='user_input' "
            "ORDER BY seq DESC LIMIT 1",
            (sess_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    return await _run(_w)


async def count_messages_before(sess_id: str, before_seq: int) -> int:
    def _w() -> int:
        cur = _conn.execute(
            "SELECT COUNT(*) FROM messages WHERE sess_id=? AND seq < ?",
            (sess_id, before_seq),
        )
        return int(cur.fetchone()[0])
    return await _run(_w)


async def max_seq(sess_id: str) -> int:
    def _w() -> int:
        cur = _conn.execute("SELECT MAX(seq) FROM messages WHERE sess_id=?",
                             (sess_id,))
        row = cur.fetchone()
        return int(row[0] or 0)
    return await _run(_w)


# ---------- permissions ----------

async def save_perm(sess_id: str, scope: str, key: str) -> None:
    def _w() -> None:
        _conn.execute(
            "INSERT OR IGNORE INTO permissions(sess_id, scope, key, ts) VALUES (?,?,?,?)",
            (sess_id, scope, key, time.time()),
        )
    await _run(_w)


async def load_perms(sess_id: str) -> list[tuple[str, str]]:
    def _w() -> list[tuple[str, str]]:
        cur = _conn.execute(
            "SELECT scope, key FROM permissions WHERE sess_id=? AND scope != 'mode'",
            (sess_id,),
        )
        return list(cur.fetchall())
    return await _run(_w)


# permissions 表复用：scope="mode" 表示 session 级权限模式；只允许一行。
# manual 模式 = 没有行。allow_all 模式 = 一行 ("mode", "allow_all")。
async def save_permission_mode(sess_id: str, mode: str) -> None:
    def _w() -> None:
        _conn.execute(
            "DELETE FROM permissions WHERE sess_id=? AND scope='mode'", (sess_id,),
        )
        if mode != "manual":
            _conn.execute(
                "INSERT OR REPLACE INTO permissions(sess_id, scope, key, ts) "
                "VALUES (?,?,?,?)",
                (sess_id, "mode", mode, time.time()),
            )
    await _run(_w)


async def load_permission_mode(sess_id: str) -> str:
    def _w() -> str:
        cur = _conn.execute(
            "SELECT key FROM permissions WHERE sess_id=? AND scope='mode' LIMIT 1",
            (sess_id,),
        )
        row = cur.fetchone()
        return row[0] if row else "manual"
    return await _run(_w)


# ---------- 启动清理 ----------

async def find_orphan_perm_reqs(sess_id: str) -> list[str]:
    """该 session 中没有对应 perm_resolved 的 perm_req 的 req_id 列表。"""
    def _w() -> list[str]:
        reqs = []
        resolved = set()
        for (p,) in _conn.execute(
            "SELECT payload FROM messages WHERE sess_id=? AND kind='perm_req'",
            (sess_id,),
        ):
            try:
                reqs.append(json.loads(p).get("req_id"))
            except Exception:
                pass
        for (p,) in _conn.execute(
            "SELECT payload FROM messages WHERE sess_id=? AND kind='perm_resolved'",
            (sess_id,),
        ):
            try:
                resolved.add(json.loads(p).get("req_id"))
            except Exception:
                pass
        return [r for r in reqs if r and r not in resolved]
    return await _run(_w)
