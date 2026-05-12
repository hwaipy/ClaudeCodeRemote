#!/usr/bin/env python3
"""M0.5 PreToolUse hook probe.

Claude 会把工具调用的 JSON 通过 stdin 传给我们，我们把 input 落到
docs/hook-calls.jsonl，然后按 env CCR_PROBE_DECISION 输出允许/拒绝。

Hook 输出协议（按 Claude Code 文档推测，本脚本就是验证它）：
- exit 0 + stdout 空：放行
- exit 0 + stdout 一段 JSON：按 JSON.decision 决定
- exit 非 0：拒绝

我们这里**总是输出 JSON**，便于看 hook 的真实合约。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG = REPO / "docs" / "hook-calls.jsonl"
DECISION = os.environ.get("CCR_PROBE_DECISION", "allow")
SCENARIO = os.environ.get("CCR_PROBE_SCENARIO", "unknown")


def log(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw}
    log({
        "_dir": "recv",
        "_ts": time.time(),
        "_scenario": SCENARIO,
        "_pid": os.getpid(),
        "_argv": sys.argv,
        "_env_keys": sorted([k for k in os.environ if k.startswith("CLAUDE") or k.startswith("CCR")]),
        "stdin": payload,
    })

    if DECISION == "allow":
        # 试两种主流 hook 决定 schema，看哪个被识别
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "allowed by ccr-probe",
            },
        }
        log({"_dir": "send", "_ts": time.time(), "_scenario": SCENARIO, "stdout": out})
        sys.stdout.write(json.dumps(out))
        return 0
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "denied by ccr-probe (test)",
            },
        }
        log({"_dir": "send", "_ts": time.time(), "_scenario": SCENARIO, "stdout": out})
        sys.stdout.write(json.dumps(out))
        return 0


if __name__ == "__main__":
    sys.exit(main())
