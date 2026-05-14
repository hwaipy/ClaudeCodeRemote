"""§15 Wide-screen layout — verify the @media (min-width: 900px) two-column
grid behaves per spec."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.pages.home_page import HomePage

HAS_SESSION = re.compile(r"\bhas-session\b")


def test_wide_shows_sidebar_and_chat_simultaneously(wide_page):
    """On ≥900px both #view-home and #view-chat are always laid out."""
    hp = HomePage(wide_page)
    hp.expect_visible()
    # Body should be in stage-app once authenticated
    expect(wide_page.locator("body")).to_have_class(re.compile(r"\bstage-app\b"))
    # Both views have non-zero size (CSS grid)
    home_box = wide_page.locator("#view-home").bounding_box()
    chat_box = wide_page.locator("#view-chat").bounding_box()
    assert home_box and home_box["width"] >= 300, home_box
    assert chat_box and chat_box["width"] >= 300, chat_box
    # Sidebar is fixed at 320px
    assert 300 <= home_box["width"] <= 360, home_box


def test_wide_chat_head_offset_by_sidebar(wide_page, spawned_session):
    """chat-head is position:fixed left:320 on wide, so its x ≥ 300."""
    sid = spawned_session(name="wide-head-test")
    hp = HomePage(wide_page)
    hp.expect_visible()
    expect(hp.card_by_id(sid)).to_be_visible(timeout=5000)
    hp.card_by_id(sid).click()
    expect(wide_page.locator("body")).to_have_class(HAS_SESSION, timeout=5000)

    bbox = wide_page.locator("#chat-head").bounding_box()
    assert bbox is not None, "#chat-head has no bbox"
    assert bbox["x"] >= 300, f"chat-head should start after sidebar, got x={bbox['x']}"


def test_wide_clicking_card_keeps_sidebar_visible(wide_page, spawned_session):
    sid = spawned_session(name="wide-click-test")
    hp = HomePage(wide_page)
    hp.expect_visible()
    hp.card_by_id(sid).click()
    expect(wide_page.locator("body")).to_have_class(HAS_SESSION, timeout=5000)
    # Sidebar must still be visible after navigating into chat
    home_box = wide_page.locator("#view-home").bounding_box()
    chat_box = wide_page.locator("#view-chat").bounding_box()
    assert home_box and home_box["width"] >= 300
    assert chat_box and chat_box["width"] >= 300


def test_narrow_view_is_single_pane(mobile_page, spawned_session):
    """On <900px only one view's .active at a time (existing mobile behavior)."""
    sid = spawned_session(name="mobile-test")
    hp = HomePage(mobile_page)
    hp.expect_visible()
    hp.card_by_id(sid).click()
    expect(mobile_page.locator("body")).to_have_class(HAS_SESSION, timeout=5000)
    expect(mobile_page.locator("#view-chat")).to_have_class(
        re.compile(r"\bactive\b")
    )
    expect(mobile_page.locator("#view-home")).not_to_have_class(
        re.compile(r"\bactive\b")
    )
