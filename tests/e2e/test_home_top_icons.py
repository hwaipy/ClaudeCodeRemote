"""Spec §2: home-top 顺序 (按 spec 行表): .home-brand 撑左 + 右侧 icon 串
#new-btn → #apps-btn (hub-only) → #settings-btn → #search-btn. 搜索打开时
所有非 search icon 同时压缩到 width 0 (动画, 不 display:none) 留 DOM 不动.
"""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.pages.home_page import HomePage


def test_three_icon_buttons_present_in_order(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    s = logged_in_page.locator("#settings-btn").bounding_box()
    n = logged_in_page.locator("#new-btn").bounding_box()
    sb = logged_in_page.locator("#search-btn").bounding_box()
    assert s and n and sb
    # spec 顺序: new → settings → search (local mode 下 apps-btn 隐藏)
    assert n["x"] < s["x"] < sb["x"], (
        f"order should be new({n['x']}) < settings({s['x']}) < search({sb['x']})"
    )
    # All three vertically aligned (centers within 6px)
    s_cy = s["y"] + s["height"] / 2
    n_cy = n["y"] + n["height"] / 2
    sb_cy = sb["y"] + sb["height"] / 2
    assert abs(s_cy - n_cy) <= 6 and abs(n_cy - sb_cy) <= 6, (
        f"three icons must share a row: cys={s_cy}/{n_cy}/{sb_cy}"
    )


def test_three_icons_share_dimensions(logged_in_page):
    """They must look like one set: same height (30) and similar width."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    boxes = [
        logged_in_page.locator(sel).bounding_box()
        for sel in ("#settings-btn", "#new-btn", "#search-btn")
    ]
    heights = [b["height"] for b in boxes]
    widths = [b["width"] for b in boxes]
    # All three within 4px in both dimensions
    assert max(heights) - min(heights) <= 4, f"heights mismatch: {heights}"
    assert max(widths) - min(widths) <= 4, f"widths mismatch: {widths}"
    # And they're round-ish (height close to width)
    for b in boxes:
        assert abs(b["width"] - b["height"]) <= 8, f"not round-ish: {b}"


def test_icons_layout_new_settings_packed_search_pushed_right(logged_in_page):
    """spec 行 743: home-top 一行 flex, .home-brand 撑左, new/settings 8px
    gap 紧贴, #search-bar 用 margin-left: auto 推到最右. 三个 icon 同一行."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    n = logged_in_page.locator("#new-btn").bounding_box()
    s = logged_in_page.locator("#settings-btn").bounding_box()
    sb = logged_in_page.locator("#search-bar").bounding_box()
    assert n and s and sb
    cy = lambda b: b["y"] + b["height"] / 2
    assert abs(cy(n) - cy(s)) <= 6 and abs(cy(s) - cy(sb)) <= 6, (
        f"icons must share a row: cys={cy(n)}/{cy(s)}/{cy(sb)}"
    )
    gap_ns = s["x"] - (n["x"] + n["width"])
    assert 0 <= gap_ns <= 14, (
        f"new → settings 应紧贴 (gap ≤ 14): {gap_ns}"
    )
    # search-bar 用 margin-left:auto 推到最右, 跟 settings 留大空隙
    gap_ss = sb["x"] - (s["x"] + s["width"])
    assert gap_ss > 50, (
        f"search-bar 应被推到右侧 (gap_ss > 50): {gap_ss}"
    )


def test_settings_btn_squeezes_with_search_open(logged_in_page):
    """Same compress-instead-of-hide animation as #new-btn — when
    .home-top.search-open, both icons collapse to 0 width but stay in DOM."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    settings_btn = logged_in_page.locator("#settings-btn")
    expect(settings_btn).to_be_visible()
    logged_in_page.locator("#search-btn").click()
    expect(logged_in_page.locator(".home-top")).to_have_class(
        re.compile(r"\bsearch-open\b"), timeout=2000
    )
    logged_in_page.wait_for_timeout(450)
    box = settings_btn.bounding_box()
    assert box and box["width"] <= 5, f"settings-btn should reach 0: {box}"
    # Still in DOM with opacity 1 — animation, not display:none.
    expect(settings_btn).to_have_css("opacity", "1")
