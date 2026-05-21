"""§2 Active 区: section header 显示 active session 数, 跟 Stash /
Inactive 区一致."""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


def test_active_section_shows_count_when_nonzero(
    logged_in_page, base_url, test_token
):
    page = logged_in_page
    hp = HomePage(page)
    hp.expect_visible()
    sids = []
    try:
        # Spawn 3 active sessions
        for i in range(3):
            sids.append(api_spawn(base_url, test_token, "/tmp", f"count-{i}"))
        # Wait for cards to render via WS
        page.wait_for_function(
            "() => document.querySelectorAll('#session-list-active .session-card').length >= 3",
            timeout=5000,
        )
        count_text = page.locator("#sessions-active .count").text_content()
        assert count_text and count_text.strip() == "(3)", (
            f"expected '(3)', got {count_text!r}"
        )
    finally:
        for sid in sids:
            api_delete_session(base_url, test_token, sid)


def test_active_count_decrements_on_stash(
    logged_in_page, base_url, test_token
):
    """Stash 一个卡 → active count 减 1, stash count 加 1."""
    page = logged_in_page
    hp = HomePage(page)
    hp.expect_visible()
    sids = []
    try:
        for i in range(2):
            sids.append(api_spawn(base_url, test_token, "/tmp", f"recount-{i}"))
        page.wait_for_function(
            "() => document.querySelectorAll('#session-list-active .session-card').length >= 2",
            timeout=5000,
        )
        assert page.locator("#sessions-active .count").text_content().strip() == "(2)"
        # Stash the first one
        first = page.locator(f"[data-id='{sids[0]}']")
        first.locator(".card-menu-btn").click()
        first.locator(".card-menu-item[data-action='stash']").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#session-list-active .session-card').length === 1",
            timeout=3000,
        )
        assert page.locator("#sessions-active .count").text_content().strip() == "(1)"
        assert page.locator("#sessions-stash .count").text_content().strip() == "(1)"
    finally:
        for sid in sids:
            try:
                api_delete_session(base_url, test_token, sid)
            except Exception:
                pass


def test_active_count_hidden_when_zero(logged_in_page):
    """没有 active session 时, count 应为空字符串 (跟 inactive/stash 行为
    一致)."""
    page = logged_in_page
    hp = HomePage(page)
    hp.expect_visible()
    count_text = page.locator("#sessions-active .count").text_content()
    assert (count_text or "").strip() == "", (
        f"empty active list: count must be empty, got {count_text!r}"
    )
