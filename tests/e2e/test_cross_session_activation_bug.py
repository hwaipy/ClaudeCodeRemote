"""BACKLOG #6: 在 session A 里提问后, 其它已 deactivate 的 session B / C
被错误地一并激活. 这个测试构造场景验证 bug 是否存在."""
from __future__ import annotations

import re
import time

import httpx
import pytest
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _sessions(base_url, token):
    r = httpx.get(
        f"{base_url}/api/sessions",
        headers={"Authorization": f"Bearer {token}"}, timeout=5,
    )
    r.raise_for_status()
    return {s["id"]: s for s in r.json()["sessions"]}


def _deactivate(base_url, token, sid):
    r = httpx.post(
        f"{base_url}/api/sessions/{sid}/deactivate",
        headers={"Authorization": f"Bearer {token}"}, timeout=5,
    )
    r.raise_for_status()


def test_sending_message_does_not_activate_other_inactive_sessions(
    logged_in_page, base_url, test_token
):
    """3 个 session A/B/C, deactivate B 和 C. 进 A 发 message.
    断言: 完成后 B 和 C 仍然 is_inactive=true. (复现 BACKLOG #6)"""
    page = logged_in_page
    a = api_spawn(base_url, test_token, "/tmp", "xact-A")
    b = api_spawn(base_url, test_token, "/tmp", "xact-B")
    c = api_spawn(base_url, test_token, "/tmp", "xact-C")
    try:
        _deactivate(base_url, test_token, b)
        _deactivate(base_url, test_token, c)
        before = _sessions(base_url, test_token)
        assert before[b]["is_inactive"] and before[c]["is_inactive"], (
            f"setup: B and C should be inactive, got {before}"
        )

        # 进 A 的聊天
        # 先要刷新让 page 接到新 session_state, 看到 A 卡片
        page.reload()
        page.wait_for_selector("#view-home.active", timeout=5000)
        card_a = page.locator(f"[data-id='{a}']")
        expect(card_a).to_be_visible(timeout=5000)
        card_a.click()
        expect(page.locator("body")).to_have_class(
            re.compile(r"\bhas-session\b"), timeout=10000
        )
        page.wait_for_timeout(500)

        # 在 A 里输入消息并按 Enter 发送
        chat_input = page.locator("#chat-input")
        chat_input.fill("ping from A")
        chat_input.press("Enter")
        # 等服务端处理 + ws 推送 session_state
        page.wait_for_timeout(2000)

        after = _sessions(base_url, test_token)
        assert after[b]["is_inactive"], (
            f"sending message in A must NOT activate B. After: "
            f"is_inactive={after[b]['is_inactive']}, full={after[b]}"
        )
        assert after[c]["is_inactive"], (
            f"sending message in A must NOT activate C. After: "
            f"is_inactive={after[c]['is_inactive']}, full={after[c]}"
        )
    finally:
        for sid in (a, b, c):
            try:
                api_delete_session(base_url, test_token, sid)
            except Exception:
                pass
