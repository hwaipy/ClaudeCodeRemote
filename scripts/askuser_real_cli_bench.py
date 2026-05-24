#!/usr/bin/env python3
"""量化真实 Claude CLI 的 AskUserQuestion 答案采纳率.

测试假设 (来自 server/api.py:480-486 docstring):
  Claude CLI 在 --print 模式下 emit AskUserQuestion tool_use 后, 自己内部
  合成一条 is_error=true content="Answer questions?" 的 tool_result 加到
  发往 Anthropic 的消息流. 我们 stdin 注入的真答案排第二. Anthropic 用
  第一条, agent 最终回的是 "Answer questions?" 而不是采纳真答案.

测试流程 (每轮):
  1. POST /api/spawn 起一个新 session
  2. WS 连 /ws/{sid}
  3. 发 user_message: "请用 AskUserQuestion 问我一道单选: 红/蓝. 我答完后
     告诉我你听到了什么颜色"
  4. 等 askuser_request frame
  5. 发 askuser_answer 选 "红"
  6. 等下一轮 assistant 回复 (result event)
  7. 看最后 assistant text 里:
     - 含 "红" → win (采纳)
     - 含 "Answer questions" / "未回答" / 类似 → lose (CLI fake 赢)
     - 既无又无 → ambiguous
  8. DELETE session 收尾

用法:
    .venv/bin/python scripts/askuser_real_cli_bench.py [N]
    默认 N=5.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter

import httpx
import websockets


SERVER = os.environ.get("SERVER_URL", "http://localhost:1884")
WS_BASE = SERVER.replace("http://", "ws://").replace("https://", "wss://")
TOKEN = os.environ.get("SERVER_TOKEN", "freespace")
CWD = os.environ.get("BENCH_CWD", "/tmp")

PROMPT = (
    "我现在要你做一个测试. 请**立刻**用 AskUserQuestion 工具问我以下问题:\n"
    "  问题: 你喜欢哪个颜色?\n"
    "  选项: A. 红色  B. 蓝色\n"
    "我答完之后, 请用一句简短的话告诉我'你回答了 X 色', 其中 X 是我选的颜色名."
    "不要做任何其他事."
)

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def _spawn_session(c: httpx.AsyncClient, name: str) -> str:
    r = await c.post(
        "/api/spawn",
        json={"cwd": CWD, "name": name, "permission_mode": "allow_all"},
        headers=HEADERS,
    )
    r.raise_for_status()
    return r.json()["id"]


async def _delete_session(c: httpx.AsyncClient, sid: str) -> None:
    try:
        await c.delete(f"/api/sessions/{sid}", headers=HEADERS, timeout=5)
    except Exception:
        pass


async def _run_one(round_idx: int) -> dict:
    sid: str | None = None
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=SERVER, timeout=60) as c:
        try:
            sid = await _spawn_session(c, f"askuser-bench-{round_idx}-{uuid.uuid4().hex[:4]}")
            ws_url = f"{WS_BASE}/ws/{sid}?token={TOKEN}"
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({"type": "user_message", "content": PROMPT}))

                # State machine: wait askuser_request -> send answer -> collect text until result
                got_askuser = False
                got_result = False
                assistant_text_chunks: list[str] = []
                askuser_text: str = ""
                tool_use_id: str | None = None
                tool_result_after_answer: str = ""
                saw_user_tool_result_for_our_tu = False

                debug = os.environ.get("DEBUG") == "1"
                deadline = time.perf_counter() + 180   # max 3 min per round
                while time.perf_counter() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=120)
                    env = json.loads(raw)
                    event = env.get("event") if isinstance(env, dict) else None
                    if not isinstance(event, dict):
                        continue
                    et, sub = event.get("type"), event.get("subtype")
                    if debug:
                        print(f"    [+{time.perf_counter()-started:5.1f}s] {et}/{sub} {str(event)[:140]}")

                    # askuser_request — 立即答 红色
                    if et == "_ccr" and sub == "askuser_request" and not got_askuser:
                        tool_use_id = event.get("tool_use_id")
                        askuser_text = json.dumps(event.get("tool_input"), ensure_ascii=False)[:400]
                        got_askuser = True
                        await ws.send(json.dumps({
                            "type": "askuser_answer",
                            "tool_use_id": tool_use_id,
                            "answers": [{"option": "A. 红色"}],
                        }))
                        continue

                    # capture text_delta chunks (server wraps everything in
                    # stream_event for real CLI; some test fixtures don't)
                    if et == "stream_event":
                        inner = event.get("event") or {}
                        if (inner.get("type") == "content_block_delta"):
                            d = inner.get("delta") or {}
                            if d.get("type") == "text_delta":
                                assistant_text_chunks.append(d.get("text", ""))
                        continue
                    if et == "content_block_delta":
                        d = event.get("delta") or {}
                        if d.get("type") == "text_delta":
                            assistant_text_chunks.append(d.get("text", ""))
                        continue

                    # user tool_result with our tool_use_id (from server-side inject)
                    if et == "user" and isinstance(event.get("message"), dict):
                        for blk in event["message"].get("content") or []:
                            if (isinstance(blk, dict)
                                    and blk.get("type") == "tool_result"
                                    and blk.get("tool_use_id") == tool_use_id):
                                saw_user_tool_result_for_our_tu = True
                                tool_result_after_answer = str(blk.get("content"))[:200]
                        continue

                    # turn end
                    if et == "result":
                        got_result = True
                        # let a brief moment for any trailing text deltas
                        await asyncio.sleep(0.5)
                        # try to drain a few more
                        try:
                            while True:
                                raw2 = await asyncio.wait_for(ws.recv(), timeout=0.3)
                                env2 = json.loads(raw2)
                                ev2 = env2.get("event") if isinstance(env2, dict) else None
                                if isinstance(ev2, dict) and ev2.get("type") == "content_block_delta":
                                    d2 = ev2.get("delta") or {}
                                    if d2.get("type") == "text_delta":
                                        assistant_text_chunks.append(d2.get("text", ""))
                        except (asyncio.TimeoutError, websockets.ConnectionClosed):
                            pass
                        break

                final_text = "".join(assistant_text_chunks).strip()
                # classify
                lower = final_text.lower()
                lose_keywords = [
                    "answer question", "未回答", "没有回答", "没有收到",
                    "取消", "未收到", "没有作答", "没作答", "没回答",
                    "我没有", "我无法", "看起来你没",
                ]
                if not got_askuser:
                    outcome = "no_askuser"  # model didn't use the tool
                elif "红" in final_text and not any(
                        k in final_text for k in ("取消", "没有回答", "没有收到", "未回答")):
                    outcome = "win"
                elif any(k in lower for k in [w.lower() for w in lose_keywords]):
                    outcome = "lose_fake_won"
                else:
                    outcome = "ambiguous"
                elapsed = time.perf_counter() - started
                return {
                    "round": round_idx,
                    "outcome": outcome,
                    "got_askuser": got_askuser,
                    "got_result": got_result,
                    "saw_inject_tool_result": saw_user_tool_result_for_our_tu,
                    "final_text": final_text[:300],
                    "askuser_q": askuser_text[:200],
                    "tool_result_after_answer": tool_result_after_answer,
                    "elapsed_s": elapsed,
                    "sid": sid,
                }
        except Exception as e:
            return {
                "round": round_idx,
                "outcome": f"exception_{type(e).__name__}",
                "error": str(e)[:200],
                "elapsed_s": time.perf_counter() - started,
                "sid": sid,
            }
        finally:
            if sid:
                await _delete_session(c, sid)


async def main(n: int) -> int:
    print(f"=== Running {n} rounds against {SERVER} ===")
    results = []
    for i in range(n):
        print(f"\n--- Round {i+1}/{n} ---")
        r = await _run_one(i)
        results.append(r)
        print(f"  outcome:    {r['outcome']}")
        print(f"  elapsed:    {r['elapsed_s']:.1f}s")
        if r.get("got_askuser") is False:
            print(f"  (model didn't call AskUserQuestion)")
        print(f"  got_askuser:{r.get('got_askuser')}  got_result:{r.get('got_result')}  inject:{r.get('saw_inject_tool_result')}")
        print(f"  final_text: {(r.get('final_text') or '<EMPTY>')[:300]}")
        if r.get("error"):
            print(f"  error:      {r['error']}")

    print("\n\n" + "=" * 50)
    outcomes = Counter(r["outcome"] for r in results)
    for o, c in outcomes.most_common():
        print(f"  {o:.<40} {c}/{n} ({100*c/n:.0f}%)")
    return 0


if __name__ == "__main__":
    n_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    sys.exit(asyncio.run(main(n_arg)))
