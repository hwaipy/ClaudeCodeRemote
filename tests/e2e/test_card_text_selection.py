"""§2 Active 区: 用户在 .session-card 里拖选文字时, mouseup 不应该
触发卡片 click → enterChat. 否则文本根本无法被复制."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage

HAS_SESSION = re.compile(r"\bhas-session\b")


def test_drag_select_inside_card_does_not_enter_chat(
    logged_in_page, base_url, test_token
):
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "selectable-text")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        card = page.locator(f"[data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        name = card.locator(".name")
        box = name.bounding_box()
        assert box, "name element should have a bounding box"

        # Drag from one end of .name to the other to select the text
        x1 = box["x"] + 2
        x2 = box["x"] + box["width"] - 2
        y = box["y"] + box["height"] / 2
        page.mouse.move(x1, y)
        page.mouse.down()
        page.mouse.move(x2, y, steps=10)
        page.mouse.up()

        # A non-empty selection should now exist
        sel = page.evaluate("() => window.getSelection().toString()")
        assert sel and sel.strip(), (
            f"expected non-empty selection after drag, got {sel!r}"
        )

        # And the click that fires on mouseup must NOT have entered chat
        page.wait_for_timeout(200)
        expect(page.locator("body")).not_to_have_class(HAS_SESSION)
    finally:
        api_delete_session(base_url, test_token, sid)


def test_plain_click_still_enters_chat(
    logged_in_page, base_url, test_token
):
    """Sanity guard: a click WITHOUT any selection still navigates."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "plain-click-text")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        card = page.locator(f"[data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        # Make sure no stale selection lingers
        page.evaluate("() => window.getSelection().removeAllRanges()")
        card.click()
        expect(page.locator("body")).to_have_class(HAS_SESSION, timeout=5000)
    finally:
        api_delete_session(base_url, test_token, sid)
