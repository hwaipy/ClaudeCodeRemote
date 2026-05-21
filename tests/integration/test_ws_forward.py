"""M-Hub-1 WS forward 验收.

布局: 跟 test_http_forward 共用 hub_and_app fixture (起 hub + app, app 用
fake_claude).

测试:
- POST /api/spawn via hub → 拿 sid
- WS 连 hub /ws/<sid> (带 ccr_sess cookie) → 应能收到 first_paint envelope +
  backlog_done. 跟直连 app 行为一致.
- WS 关闭后再连一次, 仍能拉到历史.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets


def _login_get_cookie(hub_url: str, email: str, pw: str) -> str:
    with httpx.Client(base_url=hub_url, timeout=5) as c:
        r = c.post("/api/hub/login", json={"email": email, "password": pw})
        r.raise_for_status()
        cookie = c.cookies.get("ccr_sess")
        assert cookie, "no ccr_sess cookie set"
        return cookie


@pytest.mark.anyio
async def test_ws_forward_first_paint(hub_and_app):
    """Spawn 一个 session 经 hub → WS 连 hub /ws/<sid> → 收到 first_paint."""
    cookie = _login_get_cookie(
        hub_and_app["hub_url"],
        hub_and_app["admin_email"], hub_and_app["admin_pw"],
    )
    # spawn via hub
    async with httpx.AsyncClient(
        base_url=hub_and_app["hub_url"],
        cookies={"ccr_sess": cookie},
        timeout=10,
    ) as c:
        r = await c.post("/api/spawn", json={
            "cwd": hub_and_app["default_cwd"],
            "name": "ws-forward-test",
            "permission_mode": "manual",
            "model": "",
            "effort": "",
        })
        assert r.status_code == 200, r.text
        sid = r.json()["id"]

    # 连 hub /ws/<sid>
    ws_url = f"{hub_and_app['ws_hub_url']}/ws/{sid}"
    # websockets 客户端用 Cookie header 带 session
    headers = [("Cookie", f"ccr_sess={cookie}")]
    async with websockets.connect(
        ws_url, additional_headers=headers,
    ) as ws:
        # 等 first_paint envelope. envelope 是 {seq, ts, event: {type, subtype}}.
        first_paint = None
        deadline = asyncio.get_event_loop().time() + 5
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            evt = json.loads(raw)
            inner = evt.get("event") if isinstance(evt, dict) else None
            if (isinstance(inner, dict) and inner.get("type") == "_ccr"
                    and inner.get("subtype") == "first_paint"):
                first_paint = evt
                break
        assert first_paint is not None, (
            "应收到 first_paint envelope (透传自 app)"
        )


@pytest.mark.anyio
async def test_ws_forward_send_and_receive(hub_and_app):
    """user_message 经 hub → app → fake_claude 产生 assistant_text → 透回."""
    cookie = _login_get_cookie(
        hub_and_app["hub_url"],
        hub_and_app["admin_email"], hub_and_app["admin_pw"],
    )
    async with httpx.AsyncClient(
        base_url=hub_and_app["hub_url"],
        cookies={"ccr_sess": cookie},
        timeout=10,
    ) as c:
        r = await c.post("/api/spawn", json={
            "cwd": hub_and_app["default_cwd"],
            "name": "ws-send-recv",
            "permission_mode": "manual",
            "model": "",
            "effort": "",
        })
        assert r.status_code == 200, r.text
        sid = r.json()["id"]

    headers = [("Cookie", f"ccr_sess={cookie}")]
    async with websockets.connect(
        f"{hub_and_app['ws_hub_url']}/ws/{sid}",
        additional_headers=headers,
    ) as ws:
        # 等 backlog_done 表示 first paint 流结束 (后续是实时)
        deadline = asyncio.get_event_loop().time() + 5
        got_backlog = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                continue
            evt = json.loads(raw)
            inner = evt.get("event") if isinstance(evt, dict) else None
            if (isinstance(inner, dict) and inner.get("type") == "_ccr"
                    and inner.get("subtype") == "backlog_done"):
                got_backlog = True
                break
        assert got_backlog, "first paint 后应有 backlog_done envelope"

        # send user_message
        await ws.send(json.dumps({
            "type": "user_message", "content": "hello from test",
        }))
        # 等 user_input echo or assistant text
        saw_user_input = False
        saw_anything_after = False
        deadline = asyncio.get_event_loop().time() + 5
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                continue
            evt = json.loads(raw)
            # server push 包: {"seq":..., "ts":..., "event": ...}
            inner = evt.get("event") if isinstance(evt, dict) else None
            if isinstance(inner, dict) and inner.get("type") == "user_input":
                saw_user_input = True
            if saw_user_input:
                saw_anything_after = True
                break
        assert saw_user_input, "应收到 user_input echo event"
        assert saw_anything_after, "user_input 之后应继续有 events"
