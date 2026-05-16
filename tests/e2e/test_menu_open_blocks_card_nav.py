"""§2 Active 区: 当一张 .card-menu 是 open 状态时, 用户点击 (摸到)
另一张卡 — 不应该进入那张卡的聊天, 只关掉菜单."""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage

HAS_SESSION = re.compile(r"\bhas-session\b")


def test_clicking_another_card_with_menu_open_only_closes_menu(
    logged_in_page, base_url, test_token
):
    page = logged_in_page
    sid_a = api_spawn(base_url, test_token, "/tmp", "menu-open-card")
    sid_b = api_spawn(base_url, test_token, "/tmp", "other-card")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        card_a = page.locator(f"[data-id='{sid_a}']")
        card_b = page.locator(f"[data-id='{sid_b}']")
        expect(card_a).to_be_visible(timeout=5000)
        expect(card_b).to_be_visible(timeout=5000)

        # Open A's kebab menu
        card_a.locator(".card-menu-btn").click()
        expect(card_a.locator(".card-menu:not([hidden])")).to_have_count(1)

        # Click on card B's body (not on its kebab)
        # Use the .name area — well inside the card body
        card_b.locator(".name").click()

        # Menu A must be closed
        expect(card_a.locator(".card-menu:not([hidden])")).to_have_count(0)
        # And we MUST NOT have navigated into card B's chat
        page.wait_for_timeout(200)
        expect(page.locator("body")).not_to_have_class(HAS_SESSION)
    finally:
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)


def test_tapping_another_card_with_menu_open_only_closes_menu_on_touch(
    playwright, base_url, test_token
):
    """同上, 但是在 hasTouch=true / isMobile=true 的上下文 — 之前的 fix
    在 touch 路径上失效 (setTimeout(0) 在合成 click 之前清了 flag).
    用 page.touchscreen.tap() 强制触发原生 touch 流."""
    from tests.helpers import api_spawn, api_delete_session

    sid_a = api_spawn(base_url, test_token, "/tmp", "menu-touch-a")
    sid_b = api_spawn(base_url, test_token, "/tmp", "menu-touch-b")
    browser = playwright.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True, is_mobile=True,
    )
    page = ctx.new_page()
    try:
        page.goto(base_url)
        page.fill("#login-token", test_token)
        page.click("#login-go")
        page.wait_for_selector("#view-home.active", timeout=5000)

        card_a = page.locator(f"[data-id='{sid_a}']")
        card_b = page.locator(f"[data-id='{sid_b}']")
        expect(card_a).to_be_visible(timeout=5000)
        expect(card_b).to_be_visible(timeout=5000)

        # Tap A's kebab to open menu A (real touch)
        kb_a = card_a.locator(".card-menu-btn").bounding_box()
        assert kb_a
        page.touchscreen.tap(
            kb_a["x"] + kb_a["width"] / 2,
            kb_a["y"] + kb_a["height"] / 2,
        )
        page.wait_for_timeout(50)
        expect(card_a.locator(".card-menu:not([hidden])")).to_have_count(1)

        # Tap card B's name area (real touch — fires touchstart/end)
        name_b = card_b.locator(".name").bounding_box()
        assert name_b
        page.touchscreen.tap(
            name_b["x"] + name_b["width"] / 2,
            name_b["y"] + name_b["height"] / 2,
        )
        page.wait_for_timeout(200)
        # Menu A must close
        expect(card_a.locator(".card-menu:not([hidden])")).to_have_count(0)
        # And we MUST NOT have navigated into card B's chat
        expect(page.locator("body")).not_to_have_class(HAS_SESSION)
    finally:
        ctx.close()
        browser.close()
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)


def test_clicking_card_without_menu_open_still_navigates(
    logged_in_page, base_url, test_token
):
    """Sanity: when no menu is open, a normal click still enters chat —
    we haven't broken the basic navigation."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "normal-nav")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        card = page.locator(f"[data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        card.click()
        expect(page.locator("body")).to_have_class(HAS_SESSION, timeout=5000)
    finally:
        api_delete_session(base_url, test_token, sid)
