#!/usr/bin/env python3
"""把 Claude CLI 的 session jsonl 历史导入到 CCR sqlite db.

Claude CLI 把每个 session 持久化到
    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl
每行一个 stream-json event (跟 CCR 实时收到的 stream-json 接近但不完全一样,
混了一些 CLI 内部 meta event 比如 last-prompt / queue-operation 等).

这个 script:
  1. 解析 jsonl, 过滤掉 CLI 内部 meta event
  2. 把 user/assistant/result/system_init normalize 成 CCR envelope 格式
  3. 在 CCR db 里 INSERT 一个 sessions row + 全部 messages rows
  4. 记录 claude_session_id 让 future `--resume` 接得上

usage:
    python scripts/import_claude_session.py \
        --jsonl ~/.claude/projects/-home-hwaipy-codes-GhostPaper/e23a9b89-...jsonl \
        --cwd ~/codes/GhostPaper \
        --name GhostPaper \
        [--db ~/.local/share/ccr/ccr.sqlite]
        [--ccr-session-id ccr-import-xxx]   # override default ccr-<random>

import 后 CCR 重启不需要 (db 直读). PWA 刷新 home 就能看到. 进 chat 走
backlog 渲染历史. 后端再开新 turn 时通过 update_claude_sid 拿到的
claude_session_id 调 --resume 接续.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# CLI 内部 meta event, 不入 CCR
SKIP_TYPES = {
    "last-prompt", "queue-operation", "permission-mode", "summary",
    "file-history-snapshot",
}


def parse_ts(ts_str: str | None) -> float | None:
    """ISO8601 ('2026-05-12T11:00:00.123Z') → unix float."""
    if not ts_str:
        return None
    s = ts_str.rstrip("Z")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--jsonl", required=True, help="path to claude session jsonl")
    p.add_argument("--cwd", required=True, help="session working directory")
    p.add_argument("--name", required=True, help="session display name in CCR")
    p.add_argument("--db", default=os.path.expanduser(
        "~/.local/share/ccr/ccr.sqlite"), help="CCR sqlite db path")
    p.add_argument("--ccr-session-id", default=None,
                   help="override CCR session id (default ccr-<random hex 12>)")
    p.add_argument("--dry-run", action="store_true",
                   help="parse but don't insert; print summary")
    args = p.parse_args()

    jsonl_path = Path(os.path.expanduser(args.jsonl))
    cwd = os.path.expanduser(args.cwd)
    if not jsonl_path.exists():
        print(f"error: jsonl not found: {jsonl_path}", file=sys.stderr)
        return 1

    ccr_sid = args.ccr_session_id or f"ccr-{uuid.uuid4().hex[:12]}"

    events: list[tuple[float, str, dict[str, Any]]] = []
    claude_sid: str | None = None
    cur_ts: float | None = None
    fallback_inc = 0

    def next_ts() -> float:
        nonlocal fallback_inc
        if cur_ts is None:
            return time.time()
        fallback_inc += 1
        return cur_ts + fallback_inc * 0.001

    skipped_counts: dict[str, int] = {}
    total_lines = 0

    with jsonl_path.open(encoding="utf-8") as f:
        for ln, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            total_lines += 1
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                skipped_counts["<parse-error>"] = skipped_counts.get("<parse-error>", 0) + 1
                continue
            t = e.get("type")
            if t in SKIP_TYPES:
                skipped_counts[t] = skipped_counts.get(t, 0) + 1
                continue
            # skip sidechain (subagent / 嵌套 inference)
            if e.get("isSidechain"):
                skipped_counts["<sidechain>"] = skipped_counts.get("<sidechain>", 0) + 1
                continue
            # capture claude session_id from anywhere it shows up
            sid = (e.get("sessionId")
                   or (e.get("message") or {}).get("session_id"))
            if sid and not claude_sid:
                claude_sid = sid
            # ts: 外层 timestamp 优先; 没有就 message.timestamp; 都没有就 fallback
            ts_raw = (e.get("timestamp")
                      or (e.get("message") or {}).get("timestamp"))
            ts = parse_ts(ts_raw)
            if ts is not None:
                cur_ts = ts
                fallback_inc = 0
            else:
                ts = next_ts()

            if t == "system":
                if e.get("subtype") == "init":
                    events.append((ts, "system_init", e))
                else:
                    skipped_counts[f"system/{e.get('subtype','?')}"] = (
                        skipped_counts.get(f"system/{e.get('subtype','?')}", 0) + 1)
                continue

            if t == "user":
                msg = e.get("message") or {}
                content = msg.get("content")
                # CCR frontend handleUserMessage 期望 content 是 list of blocks.
                # CLI jsonl 里 user 可能是 string (人类输入文本) 也可能是 list
                # (tool_result 注入). 统一成 list.
                if isinstance(content, str):
                    new_msg = {"role": "user",
                               "content": [{"type": "text", "text": content}]}
                else:
                    new_msg = msg
                events.append((ts, "user", {"type": "user", "message": new_msg}))
                continue

            if t == "assistant":
                msg = e.get("message") or {}
                events.append((ts, "assistant",
                                {"type": "assistant", "message": msg}))
                continue

            if t == "result":
                # 保留 result event 关键字段, 去掉 jsonl 容器 meta
                drop = {"parentUuid", "isSidechain", "userType", "entrypoint",
                        "cwd", "sessionId", "version", "gitBranch", "slug",
                        "isCompactSummary"}
                evt = {k: v for k, v in e.items() if k not in drop}
                evt["type"] = "result"
                events.append((ts, "result", evt))
                continue

            # 其它 unknown / meta type
            skipped_counts[f"<{t}>"] = skipped_counts.get(f"<{t}>", 0) + 1

    if not events:
        print("error: no convertible events found", file=sys.stderr)
        return 1

    events.sort(key=lambda x: x[0])
    created_at = events[0][0]
    last_activity_at = events[-1][0]

    summary = [
        f"ccr session id:    {ccr_sid}",
        f"claude session id: {claude_sid}",
        f"name:              {args.name}",
        f"cwd:               {cwd}",
        f"db:                {args.db}",
        f"jsonl total lines: {total_lines}",
        f"imported events:   {len(events)}",
        "skipped:",
    ]
    for k, n in sorted(skipped_counts.items(), key=lambda x: -x[1]):
        summary.append(f"  {k}: {n}")
    summary += [
        f"first event:       "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created_at))}",
        f"last event:        "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_activity_at))}",
    ]
    print("\n".join(summary))

    if args.dry_run:
        print("(dry-run, not inserting)")
        return 0

    db_path = os.path.expanduser(args.db)
    if not Path(db_path).exists():
        print(f"error: db not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    if conn.execute("SELECT 1 FROM sessions WHERE id=?",
                     (ccr_sid,)).fetchone():
        print(f"error: ccr session id collision: {ccr_sid}", file=sys.stderr)
        return 1

    conn.execute(
        "INSERT INTO sessions("
        "id, name, cwd, created_at, last_activity_at,"
        " claude_session_id, model, effort) VALUES (?,?,?,?,?,?,?,?)",
        (ccr_sid, args.name, cwd, created_at, last_activity_at,
         claude_sid or "", "", ""),
    )
    for seq, (ts, kind, payload) in enumerate(events, 1):
        conn.execute(
            "INSERT INTO messages(sess_id, seq, ts, kind, payload) "
            "VALUES (?,?,?,?,?)",
            (ccr_sid, seq, ts, kind,
             json.dumps(payload, ensure_ascii=False)),
        )
    conn.close()
    print(f"\n✓ imported {len(events)} events into CCR session {ccr_sid}")
    print("  PWA 刷新 home view 就能看到这个 session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
