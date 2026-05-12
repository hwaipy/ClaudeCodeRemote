#!/usr/bin/env python3
"""PreToolUse hook 桥接器：claude 调用我们，我们调用 server 等用户决定。

claude 通过 stdin 喂一个 JSON：
    {session_id, transcript_path, cwd, permission_mode, hook_event_name,
     tool_name, tool_input, tool_use_id, ...}

我们 POST 到 CCR_BRIDGE_URL，body 是上面 payload + ccr_session_id（环境变量）+
ccr_token。server 长轮询直到用户决定（或超时），返回：
    {"behavior": "allow"|"deny", "message"?: "...", "updatedInput"?: {...}}

我们按 PreToolUse 协议把决定输出到 stdout 后退出。失败兜底：deny。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = float(os.environ.get("CCR_BRIDGE_TIMEOUT", "600"))  # 10 分钟


def _stderr(msg: str) -> None:
    sys.stderr.write(f"[ccr-hook-bridge] {msg}\n")
    sys.stderr.flush()


def _emit(decision: str, reason: str = "") -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason or f"{decision} by ccr",
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


def main() -> int:
    url = os.environ.get("CCR_BRIDGE_URL")
    token = os.environ.get("CCR_TOKEN")
    ccr_sid = os.environ.get("CCR_SESSION_ID")
    if not url or not token or not ccr_sid:
        _stderr("missing CCR_BRIDGE_URL / CCR_TOKEN / CCR_SESSION_ID; denying")
        _emit("deny", "ccr bridge env missing")
        return 0

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        _stderr(f"bad payload from claude: {e}; denying")
        _emit("deny", "bad hook payload")
        return 0

    body = {"ccr_session_id": ccr_sid, "claude_payload": payload}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            resp_body = resp.read()
        decision_obj = json.loads(resp_body)
    except urllib.error.HTTPError as e:
        _stderr(f"server HTTP {e.code}: {e.read()[:200]!r}; denying")
        _emit("deny", f"ccr server http {e.code}")
        return 0
    except (urllib.error.URLError, TimeoutError) as e:
        _stderr(f"server unreachable / timeout: {e}; denying")
        _emit("deny", "ccr server unreachable")
        return 0
    except json.JSONDecodeError as e:
        _stderr(f"server returned non-json: {e}; denying")
        _emit("deny", "ccr server bad response")
        return 0

    behavior = decision_obj.get("behavior")
    if behavior == "allow":
        reason = decision_obj.get("message") or "user allowed"
        _emit("allow", reason)
    elif behavior == "deny":
        reason = decision_obj.get("message") or "user denied"
        _emit("deny", reason)
    else:
        _stderr(f"unknown behavior {behavior!r}; denying")
        _emit("deny", "ccr server bad behavior")
    return 0


if __name__ == "__main__":
    sys.exit(main())
