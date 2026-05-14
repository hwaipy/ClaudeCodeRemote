#!/usr/bin/env python3
"""Fake Claude CLI for tests.

Stands in for the real `claude` binary. Speaks the same flags + line-delimited
JSON protocol that CCR's ClaudeProcess._build_cmd() emits:

    --print --output-format stream-json --input-format stream-json
    --include-partial-messages --include-hook-events --verbose
    --permission-mode default [--settings PATH] [--resume SID]

Behavior is scriptable via env vars (set by the test harness):

    FAKE_CLAUDE_SCRIPT  Path to a JSONL file. Each line is a server-emitted
                        stream-json event. When the fake reads a user_message
                        on stdin, it emits the next N lines as a "response".
                        If not set, emits a stock minimal turn (system init +
                        one assistant text message + result).

    FAKE_CLAUDE_DELAY   Per-event delay in seconds (float). Default 0.

For Phase 1/2 (login tests) this fake never actually runs — no /spawn calls.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    delay = float(os.environ.get("FAKE_CLAUDE_DELAY", "0") or "0")
    if delay > 0:
        time.sleep(delay)


def stock_response(session_id: str) -> None:
    msg_id = "msg_" + uuid.uuid4().hex[:12]
    emit({"type": "message_start", "message": {
        "id": msg_id, "role": "assistant", "model": "fake-claude-1",
        "content": [], "usage": {"input_tokens": 10, "output_tokens": 0},
    }})
    emit({"type": "content_block_start", "index": 0,
          "content_block": {"type": "text", "text": ""}})
    emit({"type": "content_block_delta", "index": 0,
          "delta": {"type": "text_delta", "text": "Hello from fake claude."}})
    emit({"type": "content_block_stop", "index": 0})
    emit({"type": "message_delta",
          "delta": {"stop_reason": "end_turn"},
          "usage": {"output_tokens": 5}})
    emit({"type": "message_stop"})
    emit({"type": "result", "subtype": "success",
          "session_id": session_id,
          "total_cost_usd": 0.0001,
          "duration_ms": 10,
          "num_turns": 1,
          "is_error": False})


def main() -> int:
    session_id = "fake-" + uuid.uuid4().hex[:12]
    # Emit init event so CCR captures claude_session_id
    emit({"type": "system", "subtype": "init",
          "session_id": session_id, "model": "fake-claude-1",
          "cwd": os.getcwd(), "tools": []})

    script_path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    scripted = []
    if script_path and os.path.exists(script_path):
        with open(script_path, encoding="utf-8") as f:
            scripted = [json.loads(line) for line in f if line.strip()]

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "user":
                continue
            if scripted:
                for ev in scripted:
                    emit(ev)
            else:
                stock_response(session_id)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
