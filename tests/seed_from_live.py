#!/usr/bin/env python3
"""Snapshot live CCR data into tests/fixtures/live/ for test consumption.

Run once (or whenever you want to refresh the fixture):

    python3 tests/seed_from_live.py

What it copies:
    ~/.local/share/ccr/ccr.sqlite          → tests/fixtures/live/ccr.sqlite
    ~/.claude/projects/<encoded>/<sid>.jsonl → tests/fixtures/live/claude/projects/...
                                                (only for sessions present in the DB)

WAL safety:
    Uses sqlite3.Connection.backup() so an actively-running CCR isn't disturbed
    and the snapshot is consistent.

The whole tests/fixtures/ tree is .gitignored — real chat content stays local.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path


HOME = Path.home()
DEFAULT_LIVE_DB = HOME / ".local/share/ccr/ccr.sqlite"
DEFAULT_PROJECTS = HOME / ".claude/projects"
DEFAULT_OUT = Path(__file__).resolve().parent / "fixtures" / "live"


def snapshot_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    with sqlite3.connect(src) as src_conn, sqlite3.connect(dst) as dst_conn:
        src_conn.backup(dst_conn)
    # remove any WAL/SHM that backup() might leave on the dst side
    for ext in ("-wal", "-shm"):
        side = dst.with_name(dst.name + ext)
        if side.exists():
            side.unlink()


def copy_jsonls(db: Path, projects_src: Path, projects_dst: Path,
                skip_missing: bool = True) -> tuple[int, int]:
    """Copy each session's claude jsonl. Returns (copied, missing)."""
    copied = 0
    missing = 0
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT id, claude_session_id, cwd FROM sessions"):
            csid = row["claude_session_id"]
            cwd = row["cwd"]
            if not csid:
                continue
            encoded = cwd.replace("/", "-")
            src = projects_src / encoded / f"{csid}.jsonl"
            if not src.exists():
                missing += 1
                if not skip_missing:
                    print(f"  missing: {src}", file=sys.stderr)
                continue
            dst = projects_dst / encoded / f"{csid}.jsonl"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    return copied, missing


def summarize(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        s_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        m_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        p_count = conn.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
    print(f"  sessions:    {s_count}")
    print(f"  messages:    {m_count}")
    print(f"  permissions: {p_count}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-db", type=Path, default=DEFAULT_LIVE_DB,
                    help="path to the live CCR sqlite (default: %(default)s)")
    ap.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS,
                    help="path to ~/.claude/projects (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="output dir for snapshot (default: %(default)s)")
    ap.add_argument("--db-only", action="store_true",
                    help="skip copying claude jsonl files")
    args = ap.parse_args()

    if not args.live_db.exists():
        print(f"FATAL: live db not found: {args.live_db}", file=sys.stderr)
        return 2

    dst_db = args.out / "ccr.sqlite"
    print(f"Snapshotting DB → {dst_db}")
    snapshot_db(args.live_db, dst_db)
    summarize(dst_db)

    if not args.db_only:
        if not args.projects.exists():
            print(f"WARN: projects dir not found, skipping jsonl: {args.projects}",
                  file=sys.stderr)
        else:
            projects_dst = args.out / "claude" / "projects"
            print(f"Copying claude jsonls → {projects_dst}")
            copied, missing = copy_jsonls(dst_db, args.projects, projects_dst)
            print(f"  copied:  {copied} files")
            print(f"  missing: {missing} sessions (no jsonl on disk)")

    print(f"\nFixture ready at: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
