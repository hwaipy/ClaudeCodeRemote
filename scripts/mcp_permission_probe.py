#!/usr/bin/env python3
"""M0.5 最小 MCP server，stdio JSON-RPC。

Expose 一个 `permission_prompt` 工具。每次 tools/call 把收到的 params 原样落到
docs/mcp-calls.jsonl，并返回固定 allow 或 deny（由 env CCR_PROBE_DECISION 控制）。

启动方式（被 claude spawn）：
    claude --mcp-config '{...}' --permission-prompt-tool mcp__ccr__permission_prompt ...
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG = REPO / "docs" / "mcp-calls.jsonl"
DECISION = os.environ.get("CCR_PROBE_DECISION", "allow")
SCENARIO = os.environ.get("CCR_PROBE_SCENARIO", "unknown")


def log(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def send(msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    log({"_dir": "send", "_ts": time.time(), "_scenario": SCENARIO, **msg})


def err(s: str) -> None:
    sys.stderr.write(f"[mcp-probe] {s}\n")
    sys.stderr.flush()


def handle(req: dict) -> None:
    method = req.get("method")
    rid = req.get("id")

    if rid is None:  # notification
        err(f"notification: {method}")
        return

    if method == "initialize":
        send({
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ccr-probe", "version": "0.0.1"},
            },
        })
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "tools": [{
                    "name": "permission_prompt",
                    "description": "Probe — answers all requests with the env-fixed decision.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "input": {"type": "object"},
                            "tool_use_id": {"type": "string"},
                        },
                    },
                }],
            },
        })
    elif method == "tools/call":
        params = req.get("params") or {}
        args = params.get("arguments") or {}
        err(f"tools/call name={params.get('name')} arg_keys={list(args.keys())} decision={DECISION}")
        if DECISION == "allow":
            payload = {
                "behavior": "allow",
                "updatedInput": args.get("input", {}),
            }
        else:
            payload = {
                "behavior": "deny",
                "message": "denied by ccr-probe (test)",
            }
        send({
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            },
        })
    else:
        send({
            "jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


def main() -> int:
    err(f"started pid={os.getpid()} decision={DECISION} scenario={SCENARIO}")
    log({"_dir": "start", "_ts": time.time(), "_scenario": SCENARIO,
         "pid": os.getpid(), "decision": DECISION})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            err(f"non-json stdin: {line!r}")
            continue
        log({"_dir": "recv", "_ts": time.time(), "_scenario": SCENARIO, **req})
        try:
            handle(req)
        except Exception as e:
            err(f"handler error: {e!r}")
    log({"_dir": "exit", "_ts": time.time(), "_scenario": SCENARIO})
    return 0


if __name__ == "__main__":
    sys.exit(main())
