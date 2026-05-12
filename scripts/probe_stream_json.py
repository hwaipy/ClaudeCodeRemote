#!/usr/bin/env python3
"""M0 协议侦察：把一条 user message 喂给 stream-json 模式的 claude，
把所有 stdout 事件按行追加到 docs/stream-json-raw.jsonl，stderr 落到
docs/stream-json-stderr.log。退出后打印事件类型统计。

不假设事件结构，原样保留。本脚本就是 M0 fixture 的"采集器"。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "docs" / "stream-json-raw.jsonl"
ERR = REPO / "docs" / "stream-json-stderr.log"


def build_user_msg(text: str) -> dict:
    # SDK 风格；若不被接受，会从 stderr 看到错误，再调整。
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, help="场景标签，例如 plain_chat")
    ap.add_argument("--prompt", required=True, help="发给 claude 的 user 消息")
    ap.add_argument("--cwd", default="/tmp/ccr-probe")
    ap.add_argument(
        "--permission-mode",
        default=None,
        choices=[None, "default", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan"],
    )
    ap.add_argument("--allowed-tools", default=None, help="逗号/空格分隔")
    ap.add_argument("--disallowed-tools", default=None)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument(
        "--model",
        default=None,
        help="模型别名或全名，默认让 CLI 自己挑",
    )
    args = ap.parse_args()

    cwd = Path(args.cwd).expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    RAW.parent.mkdir(parents=True, exist_ok=True)

    claude = shutil.which("claude") or "/home/hwaipy/.local/nodejs/bin/claude"
    cmd = [
        claude,
        "--print",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--include-partial-messages",
        "--include-hook-events",
        "--verbose",
    ]
    if args.permission_mode:
        cmd += ["--permission-mode", args.permission_mode]
    if args.allowed_tools:
        cmd += ["--allowedTools", args.allowed_tools]
    if args.disallowed_tools:
        cmd += ["--disallowedTools", args.disallowed_tools]
    if args.model:
        cmd += ["--model", args.model]

    print(f"[probe] scenario={args.scenario} cwd={cwd}")
    print(f"[probe] cmd={' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    user_msg = build_user_msg(args.prompt)
    try:
        proc.stdin.write(json.dumps(user_msg, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
    except BrokenPipeError:
        pass

    started = time.monotonic()
    counter: Counter = Counter()
    nonjson_lines = 0

    with RAW.open("a", encoding="utf-8") as raw_f:
        # 标记一段开始，方便日后切片
        marker = {
            "_probe": "scenario_start",
            "scenario": args.scenario,
            "ts": time.time(),
            "cmd": cmd,
            "prompt": args.prompt,
        }
        raw_f.write(json.dumps(marker, ensure_ascii=False) + "\n")
        raw_f.flush()

        while True:
            if time.monotonic() - started > args.timeout:
                print(f"[probe] timeout after {args.timeout}s, killing", file=sys.stderr)
                proc.kill()
                break
            line = proc.stdout.readline()
            if line == "":
                if proc.poll() is not None:
                    break
                continue
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                nonjson_lines += 1
                raw_f.write(json.dumps(
                    {"_probe": "nonjson_stdout", "scenario": args.scenario, "raw": line},
                    ensure_ascii=False,
                ) + "\n")
                raw_f.flush()
                continue
            tagged = {"_scenario": args.scenario, **evt}
            raw_f.write(json.dumps(tagged, ensure_ascii=False) + "\n")
            raw_f.flush()
            # 事件类型统计：优先 type；嵌套 message 也记一下
            t = evt.get("type", "<no-type>")
            counter[t] += 1
            if t == "stream_event":
                sub = (evt.get("event") or {}).get("type") or "<no-subtype>"
                counter[f"stream_event/{sub}"] += 1

        marker_end = {
            "_probe": "scenario_end",
            "scenario": args.scenario,
            "ts": time.time(),
            "returncode": proc.returncode,
        }
        raw_f.write(json.dumps(marker_end, ensure_ascii=False) + "\n")

    stderr_data = proc.stderr.read() if proc.stderr else ""
    if stderr_data:
        with ERR.open("a", encoding="utf-8") as ef:
            ef.write(f"\n===== scenario={args.scenario} ts={int(time.time())} rc={proc.returncode} =====\n")
            ef.write(stderr_data)
            if not stderr_data.endswith("\n"):
                ef.write("\n")

    print(f"[probe] returncode={proc.returncode}")
    print(f"[probe] nonjson_lines={nonjson_lines}")
    print("[probe] event type counts:")
    for t, n in counter.most_common():
        print(f"  {n:>4}  {t}")
    if stderr_data:
        print(f"[probe] stderr captured ({len(stderr_data)} bytes) → {ERR}")

    return 0 if proc.returncode in (0, None) else proc.returncode


if __name__ == "__main__":
    sys.exit(main())
