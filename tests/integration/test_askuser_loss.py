"""量化 AskUserQuestion 答案丢失率 — server↔SPA WS 链路.

测试目的: 验证 server↔SPA 这段链路在 N 次 askuser flow 中是否丢答案.
排除 (或确认) `server/api.py:480-486` docstring 描述的
"CLI 内部 race 让我们 stdin 注入的真答案被 CLI 自合成的 fake 覆盖"
这个 known limitation 是否还伴随 server 链路 bug.

跑法:
    cd ~/codes/ClaudeCodeRemoteAutoTest/ClaudeCodeRemote
    .venv/bin/pytest tests/integration/test_askuser_loss.py -s
    # 改次数: ASKUSER_N=50 pytest ...

每个测试默认跑 20 次, 打印 success rate. 不 hard-fail (除非全失败).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import Counter

import httpx
import pytest
import websockets


N_RUNS = int(os.environ.get("ASKUSER_N", "20"))


async def _trigger_and_answer(
    app_url: str,
    ws_url: str,
    sid: str,
    *,
    hook_token: str,                # app server CCR_TOKEN, hook bridge 用
    cookies: dict | None = None,
    token: str | None = None,
) -> tuple[str, float]:
    """跑一次 askuser flow.

    流程:
      1. 连 WS 到 sid
      2. POST /api/permission/wait 给 app server, 模拟 PreToolUse hook 触发
         AskUserQuestion (server 会挂起这个 POST, 等 askuser_answer 来 resolve)
      3. WS 接收 askuser_request frame
      4. WS 发送 askuser_answer
      5. 等 POST 返回, 看 behavior 是不是 "allow"

    返回 (outcome, latency_seconds).
    outcome ∈ {"success", "hook_timeout", "no_askuser_request",
               "hook_<status>_<msg>", "exception_<type>"}
    """
    tool_use_id = "tu_" + uuid.uuid4().hex[:12]
    askuser_input = {
        "questions": [{
            "question": "Test?",
            "header": "Test",
            "options": [{"label": "Yes", "description": "yes"}],
            "multiSelect": False,
        }]
    }

    if token:
        ws_url_full = f"{ws_url}/ws/{sid}?token={token}"
        extra_headers = None
    else:
        ws_url_full = f"{ws_url}/ws/{sid}"
        cookie_str = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
        extra_headers = [("Cookie", cookie_str)] if cookie_str else None

    t0 = time.perf_counter()
    debug = os.environ.get("ASKUSER_DEBUG") == "1"
    try:
        connect_kw = {}
        if extra_headers is not None:
            connect_kw["additional_headers"] = extra_headers
        async with websockets.connect(ws_url_full, **connect_kw) as ws:
            if debug: print(f"  [dbg] ws connected, posting hook tu={tool_use_id}")

            async def hook_call() -> httpx.Response:
                async with httpx.AsyncClient(timeout=120) as hc:
                    return await hc.post(
                        f"{app_url}/api/permission/wait",
                        headers={"Authorization": f"Bearer {hook_token}"},
                        json={
                            "ccr_session_id": sid,
                            "claude_payload": {
                                "tool_name": "AskUserQuestion",
                                "tool_input": askuser_input,
                                "tool_use_id": tool_use_id,
                            },
                        },
                    )

            hook_task = asyncio.create_task(hook_call())

            # Drain frames until we see our askuser_request
            frames_seen = []
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    env = json.loads(raw)
                    if debug: print(f"  [dbg] ws frame: {str(env)[:180]}")
                    event = env.get("event") if isinstance(env, dict) else None
                    if not isinstance(event, dict):
                        continue
                    frames_seen.append(f"{event.get('type')}/{event.get('subtype','')}")
                    if (event.get("type") == "_ccr"
                            and event.get("subtype") == "askuser_request"
                            and event.get("tool_use_id") == tool_use_id):
                        break
            except asyncio.TimeoutError:
                # 同时看 hook task 状态
                hook_done = hook_task.done()
                if hook_done:
                    try:
                        r = hook_task.result()
                        hook_state = f"done {r.status_code} {r.text[:80]}"
                    except Exception as e:
                        hook_state = f"raised {type(e).__name__} {str(e)[:80]}"
                else:
                    hook_state = "pending"
                if debug:
                    print(f"  [dbg] timeout waiting for askuser_request; hook={hook_state}; frames_seen={frames_seen}")
                hook_task.cancel()
                try:
                    await hook_task
                except BaseException:
                    pass
                return f"no_askuser_request[hook={hook_state}, frames={frames_seen}]"[:200], time.perf_counter() - t0

            await ws.send(json.dumps({
                "type": "askuser_answer",
                "tool_use_id": tool_use_id,
                "answers": [{"option": "Yes"}],
            }))

            try:
                resp = await asyncio.wait_for(hook_task, timeout=20)
            except asyncio.TimeoutError:
                return "hook_timeout", time.perf_counter() - t0

            lat = time.perf_counter() - t0
            if resp.status_code != 200:
                return f"hook_{resp.status_code}", lat
            body = resp.json()
            beh = body.get("behavior")
            if beh == "allow":
                return "success", lat
            return f"hook_{beh}_{(body.get('message') or '')[:30]}", lat
    except Exception as e:
        return f"exception_{type(e).__name__}_{str(e)[:80]}", time.perf_counter() - t0


def _summarize(results: list[tuple[str, float]]) -> str:
    outcomes = Counter(o for o, _ in results)
    total = len(results)
    lines = [f"\n=== {total} runs ==="]
    for o, c in outcomes.most_common():
        lines.append(f"  {o:.<50} {c:3d} ({100*c/total:5.1f}%)")
    lats = sorted(l for o, l in results if o == "success")
    if lats:
        p50 = lats[len(lats)//2]
        p95 = lats[max(0, int(len(lats)*0.95)-1)]
        lines.append(f"  success latency: p50={p50*1000:.0f}ms p95={p95*1000:.0f}ms")
    return "\n".join(lines)


def test_askuser_local_loss(hub_and_app):
    """直连 app server (跳过 hub). 测 server↔SPA 自身 askuser 链路丢失率."""
    app_url = hub_and_app["app_url"]
    app_token = hub_and_app["app_token"]
    ws_app = app_url.replace("http://", "ws://")

    # Spawn 一个 session (reuse 跨多次 askuser, 不每次新 spawn)
    with httpx.Client(base_url=app_url, timeout=10) as c:
        r = c.post(
            "/api/spawn",
            json={"cwd": hub_and_app["default_cwd"],
                  "name": "askuser-bench-local",
                  "permission_mode": "manual"},
            headers={"Authorization": f"Bearer {app_token}"},
        )
        assert r.status_code == 200, f"spawn failed: {r.status_code} {r.text}"
        sid = r.json()["id"]

    async def run_all():
        results = []
        for i in range(N_RUNS):
            outcome, lat = await _trigger_and_answer(
                app_url=app_url, ws_url=ws_app, sid=sid,
                token=app_token, hook_token=app_token,
            )
            results.append((outcome, lat))
            if outcome != "success":
                print(f"  trial {i}: {outcome} ({lat:.2f}s)")
        return results

    results = asyncio.run(run_all())
    print(_summarize(results))
    successes = sum(1 for o, _ in results if o == "success")
    assert successes >= 1, f"all {N_RUNS} runs failed:\n{_summarize(results)}"


def test_askuser_hub_loss(hub_and_app):
    """走 hub forward. 测 hub WS 链路是否额外丢答案."""
    hub_url = hub_and_app["hub_url"]
    ws_hub = hub_and_app["ws_hub_url"]
    app_url = hub_and_app["app_url"]
    app_id = hub_and_app["app_id"]

    with httpx.Client(base_url=hub_url, timeout=10) as c:
        r = c.post("/api/hub/login", json={
            "email": hub_and_app["admin_email"],
            "password": hub_and_app["admin_pw"],
        })
        assert r.status_code == 200, f"hub login failed: {r.text}"
        cookies = dict(c.cookies)
        # spawn via hub forward (forwarder picks app by app_id if given)
        body = {"cwd": hub_and_app["default_cwd"],
                "name": "askuser-bench-hub",
                "permission_mode": "manual"}
        # ForwardMiddleware POST /api/spawn: picks first online app for user.
        # In single-app fixture, that's our app. If multi-app, add app_id.
        r = c.post("/api/spawn", json=body)
        assert r.status_code == 200, f"hub spawn failed: {r.status_code} {r.text}"
        sid = r.json()["id"]

    async def run_all():
        results = []
        for i in range(N_RUNS):
            # hook POST 仍直 app (hook 在 app server 进程内, 不走 hub).
            # WS 走 hub.
            outcome, lat = await _trigger_and_answer(
                app_url=app_url, ws_url=ws_hub, sid=sid, cookies=cookies,
                hook_token=hub_and_app["app_token"],
            )
            results.append((outcome, lat))
            if outcome != "success":
                print(f"  trial {i}: {outcome} ({lat:.2f}s)")
        return results

    results = asyncio.run(run_all())
    print(_summarize(results))
    successes = sum(1 for o, _ in results if o == "success")
    assert successes >= 1, f"all {N_RUNS} runs failed:\n{_summarize(results)}"
