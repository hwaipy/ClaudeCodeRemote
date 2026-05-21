"""§4 工具组合并不变性: 连续工具调用必须并入同一 .tool-group, 即使
state.currentToolGroup 因 loadEarlierHistory / restoreSessionCache 等
DOM 操作错位指向中部, 新的 tool_use 仍要并入 DOM 末尾的 tool-group."""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    page.wait_for_timeout(400)


def test_new_tool_joins_last_dom_group_not_stale_state_ref(
    logged_in_page, base_url, test_token
):
    """复现 bug: chat-log 末尾有 tool-group-A, 但 state.currentToolGroup
    指向 DOM 中部的 tool-group-earlier (模拟 loadEarlierHistory 副作用).
    调 getOrCreateToolGroup 应该返回 A, 而不是新建 group B."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tool-no-split")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            // 模拟 chat-log: [earlier-group, bubble, A]
            const earlier = document.createElement('div');
            earlier.className = 'tool-group collapsed';
            earlier.id = '__earlier-group';
            log.appendChild(earlier);
            const bubble = document.createElement('div');
            bubble.className = 'bubble assistant';
            log.appendChild(bubble);
            const groupA = document.createElement('div');
            groupA.className = 'tool-group collapsed';
            groupA.id = '__group-A';
            log.appendChild(groupA);
            // 模拟 loadEarlierHistory 副作用: state 指向 earlier (而非 A)
            state.currentToolGroup = earlier;
            // 触发新工具调用 → getOrCreateToolGroup
            const got = getOrCreateToolGroup();
            return {
              gotIsA: got === groupA,
              gotId: got.id,
              totalGroups: log.querySelectorAll(':scope > .tool-group').length,
            };
          }
        """)
        assert result["gotIsA"], (
            f"getOrCreateToolGroup must reuse DOM-bottom group A, "
            f"got {result['gotId']!r}, total groups in log = {result['totalGroups']}"
        )
        assert result["totalGroups"] == 2, (
            f"must not create a 3rd group; got {result['totalGroups']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_no_tool_group_at_bottom_creates_new_one(
    logged_in_page, base_url, test_token
):
    """对照: 如果 DOM 末尾不是 tool-group (e.g. 末尾是 .bubble),
    getOrCreateToolGroup 应正常新建一个 group."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tool-create-new")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            const bubble = document.createElement('div');
            bubble.className = 'bubble user';
            log.appendChild(bubble);
            state.currentToolGroup = null;
            const got = getOrCreateToolGroup();
            return {
              gotIsGroup: got && got.classList.contains('tool-group'),
              isLast: log.lastElementChild === got,
              totalGroups: log.querySelectorAll(':scope > .tool-group').length,
            };
          }
        """)
        assert result["gotIsGroup"], "must create a new tool-group"
        assert result["isLast"], "new group should be at the bottom"
        assert result["totalGroups"] == 1, (
            f"should have exactly 1 group, got {result['totalGroups']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
