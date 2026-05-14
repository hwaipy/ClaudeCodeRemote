"""端到端测一下 AskUserQuestion 流：
1. POST /api/spawn 起 session
2. 连 WS, 发个让 claude 调 AskUserQuestion 的提示
3. 收到 assistant 含 AskUserQuestion 工具时，发 askuser_answer 回去
4. 等 server 状态回 idle，验证 claude 跑完
最后用 DELETE /api/sessions/{id} 清掉测试 session
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import websockets

TOKEN = os.environ.get("CCR_TOKEN", "freespace")
BASE  = os.environ.get("CCR_BASE", "http://127.0.0.1:1881/remote")
WS_BASE = BASE.replace("http://", "ws://").replace("https://", "wss://")
HEAD = {"Authorization": f"Bearer {TOKEN}"}

PROMPT = (
    "Please use the AskUserQuestion tool to ask me one question with three "
    "options (Red, Blue, Green). Don't try to guess; really call the tool."
)


async def main():
    cwd = str(Path(__file__).resolve().parent.parent)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{BASE}/api/spawn", headers=HEAD,
                            json={"cwd": cwd, "name": "askuser-test"})
        r.raise_for_status()
        sess = r.json()
        sid = sess["id"]
        print(f"[test] spawned session {sid}")
        try:
            await drive_session(sid)
        finally:
            r = await http.delete(f"{BASE}/api/sessions/{sid}", headers=HEAD)
            print(f"[test] session deleted, status={r.status_code}")


async def drive_session(sid):
    ws_url = f"{WS_BASE}/ws/{sid}?token={TOKEN}"
    async with websockets.connect(ws_url) as ws:
        prompt_sent = False
        seen_tool = None
        seen_askuser_request = False
        answered = False
        autofail_seen = False
        result_count = 0
        deadline = time.time() + 60

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            except asyncio.TimeoutError:
                if not prompt_sent:
                    await ws.send(json.dumps({"type": "user_message", "content": PROMPT}))
                    prompt_sent = True
                    print("[test] prompt sent (after idle)")
                continue
            env = json.loads(raw)
            ev = env.get("event") or {}
            t = ev.get("type")
            sub = ev.get("subtype")
            if t == "_ccr" and sub == "backlog_done" and not prompt_sent:
                await ws.send(json.dumps({"type": "user_message", "content": PROMPT}))
                prompt_sent = True
                print("[test] prompt sent after backlog_done")
                continue
            if t == "_ccr" and sub == "askuser_request":
                seen_askuser_request = True
                inp = ev.get("tool_input") or {}
                tid = ev.get("tool_use_id")
                qs = inp.get("questions") or []
                q0 = qs[0] if qs else {}
                print(f"[test] _ccr askuser_request:")
                print(f"        tool_use_id={tid}")
                print(f"        question: {q0.get('question')!r}")
                print(f"        options: {[o.get('label') for o in (q0.get('options') or [])]}")
                if not answered and tid:
                    # 拖 2 秒模拟用户思考
                    print("[test] sleeping 2s then submit 'Blue'")
                    await asyncio.sleep(2)
                    await ws.send(json.dumps({
                        "type": "askuser_answer",
                        "tool_use_id": tid,
                        "answers": [{"option": "Blue"}],
                    }))
                    answered = True
                    print("[test] submitted")
                continue
            if t == "assistant":
                m = ev.get("message") or {}
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" \
                       and b.get("name") == "AskUserQuestion":
                        seen_tool = b
                        inp = b.get("input") or {}
                        print(f"[test] AskUserQuestion tool_use received:")
                        print(f"        id={b.get('id')}")
                        print(f"        input keys={list(inp.keys())}")
                        if "__ccr_lazy" in inp:
                            print("        ❌ FAIL: input was lazy-stripped (should not be)")
                        qs = inp.get("questions") or []
                        if qs:
                            print(f"        question: {qs[0].get('question')!r}")
                            print(f"        options: {[o.get('label') for o in (qs[0].get('options') or [])]}")
                        # 新流程下不再这里 submit，等待 _ccr askuser_request（更早到）
                        print("[test] (assistant tool_use already happened; askuser_request should have been earlier)")
            if t == "user":
                m = ev.get("message") or {}
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        cs = c if isinstance(c, str) else json.dumps(c)
                        is_err = b.get("is_error")
                        if is_err and isinstance(c, str) and c == "Answer questions?":
                            autofail_seen = True
                            print(f"[test] ⚠ CLI auto-fail leaked through:")
                        else:
                            print(f"[test] user/tool_result (our answer echo):")
                        print(f"        tid={b.get('tool_use_id')}")
                        print(f"        is_error={is_err}")
                        print(f"        content={cs[:200]!r}")
            if t == "result":
                result_count += 1
                print(f"[test] result event #{result_count} subtype={sub} stop_reason={ev.get('stop_reason')}")
                if answered:
                    if autofail_seen:
                        print("[test] PARTIAL: claude finished but auto-fail leaked to WS")
                    elif not seen_askuser_request:
                        print("[test] PARTIAL: claude finished, but no _ccr askuser_request was seen (old path?)")
                    else:
                        print("[test] PASS: claude finished after our answer; auto-fail was suppressed")
                    return
        print("[test] ❌ FAIL: timeout reached")


asyncio.run(main())
