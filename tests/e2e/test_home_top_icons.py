"""Spec §2: home-top has three round icon buttons of the same shape:
#settings-btn (left), #new-btn (middle-ish), #search-btn (right).
When search opens, both #settings-btn AND #new-btn squeeze to width 0
(animation, not display:none) — the icons stay in DOM.
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
    # Left → right order: settings < new < search
    assert s["x"] < n["x"] < sb["x"], (
        f"order should be settings({s['x']}) < new({n['x']}) < search({sb['x']})"
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


def test_settings_and_new_packed_left_search_pushed_right(logged_in_page):
    """settings + new should be adjacent on the LEFT (gap ≤ ~10px);
    search-bar should sit on the RIGHT, separated by a large gap."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    s = logged_in_page.locator("#settings-btn").bounding_box()
    n = logged_in_page.locator("#new-btn").bounding_box()
    sb = logged_in_page.locator("#search-bar").bounding_box()
    assert s and n and sb
    # settings → new gap: just the 8px flex gap, so right_of_settings + 8 ≈ x_of_new
    gap_left = n["x"] - (s["x"] + s["width"])
    assert 0 <= gap_left <= 14, (
        f"settings and new should be packed left (gap ≤ 14): {gap_left}"
    )
    # new → search-bar gap: the auto margin, so this should be LARGE (more than half the row)
    gap_right = sb["x"] - (n["x"] + n["width"])
    assert gap_right > 100, (
        f"search should be pushed right (gap > 100): {gap_right}"
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
