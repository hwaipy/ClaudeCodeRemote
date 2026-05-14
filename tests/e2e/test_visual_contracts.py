"""Visual contracts — computed-style assertions that encode the spec mockups.

Why this file exists:
    Behavioral tests prove things WORK (elements visible, clicks fire, data
    flows). They don't catch "looks wrong relative to the mockup" — three
    bordered boxes pass the same locator checks as one unified pill.

    These tests assert the visual intent that the SVG mockups in SPEC.html
    promise. If a mockup says "single rounded pill", a test here says
    "border-radius >= 16 AND child input has no border".

How to add:
    For each visual contract that's worth locking in (i.e., would surprise
    you if regressed), add a function. Lean on `computed()` / `computed_px()`
    helpers. Keep assertions about INTENT, not exact pixels — e.g.,
    `>= 16` not `== 18`, `< 14` not `== 11`.

When NOT to add a contract:
    - Cosmetic shades / typography you'd happily tweak
    - Layout that depends on dynamic content (long names, large counts)
    - Anything you'd rather screenshot-diff than computed-style
"""
from __future__ import annotations

import re

from playwright.sync_api import Page, Locator, expect

from tests.pages.home_page import HomePage


# ---------- helpers ----------

def computed(page: Page, loc: Locator, prop: str) -> str:
    return page.evaluate(
        "({el, p}) => getComputedStyle(el).getPropertyValue(p)",
        {"el": loc.element_handle(), "p": prop},
    )


def computed_px(page: Page, loc: Locator, prop: str) -> float:
    """Computed value in pixels. Handles 'Npx' and (for radii / shorthand) '%'
    relative to the element's width."""
    raw = computed(page, loc, prop).strip()
    if not raw or raw == "auto":
        return 0.0
    if raw.endswith("%"):
        pct = float(raw.rstrip("%")) / 100.0
        box = loc.bounding_box()
        if box is None:
            return 0.0
        return pct * box["width"]
    return float(raw.rstrip("px"))


def has_border(page: Page, loc: Locator) -> bool:
    """True if any side of the border has non-zero width."""
    for prop in ("border-top-width", "border-right-width",
                 "border-bottom-width", "border-left-width"):
        if computed_px(page, loc, prop) > 0:
            return True
    return False


# ---------- §1 login ----------

def test_login_card_is_centered_and_compact(fresh_page):
    """Spec §1: login card is centered, not full-page-wide."""
    card = fresh_page.locator("#view-login .card")
    expect(card).to_be_visible()
    box = card.bounding_box()
    vp = fresh_page.viewport_size
    assert box is not None
    # Card narrower than viewport with margin both sides
    assert box["width"] < vp["width"] * 0.9
    # Roughly horizontally centered
    left_margin = box["x"]
    right_margin = vp["width"] - (box["x"] + box["width"])
    assert abs(left_margin - right_margin) < 30, (
        f"login card not centered: left={left_margin} right={right_margin}"
    )


# ---------- §2 home: home-top ----------

def test_new_session_button_is_padded(logged_in_page):
    """Spec §2.1: New session is a real button, not a bare link."""
    btn = logged_in_page.locator("#new-btn")
    expect(btn).to_be_visible()
    # Padding present horizontally (button shape, not naked text)
    assert computed_px(logged_in_page, btn, "padding-left") >= 10
    assert computed_px(logged_in_page, btn, "padding-right") >= 10
    # Has a background fill (not transparent)
    bg = computed(logged_in_page, btn, "background-color")
    assert bg not in ("rgba(0, 0, 0, 0)", "transparent"), bg


def test_search_button_is_round(logged_in_page):
    """Spec §2.1: the lens 🔍 button is a round chip."""
    btn = logged_in_page.locator("#search-btn")
    box = btn.bounding_box()
    assert box is not None
    # Square-ish
    assert abs(box["width"] - box["height"]) <= 4
    radius = computed_px(logged_in_page, btn, "border-top-left-radius")
    assert radius >= box["width"] / 2 - 2, (
        f"search-btn not round enough: radius={radius}, w={box['width']}"
    )


def test_sort_button_is_round_icon_only(logged_in_page):
    """Spec §2.1: sort button is a round chip, icon only (no text)."""
    btn = logged_in_page.locator("#sessions-sort")
    box = btn.bounding_box()
    assert box is not None
    assert abs(box["width"] - box["height"]) <= 4
    assert box["width"] <= 40, f"sort button too wide ({box['width']}) for icon-only"
    # No visible text label
    text = btn.inner_text().strip()
    assert text == "", f"sort button should be icon-only, has text: {text!r}"


# ---------- §2.1b search bar (the "single pill") ----------

def test_search_bar_is_a_single_pill(logged_in_page):
    """Spec §2.1b: the expanded search bar is ONE rounded container, with
    icon / input / close inside borderless. Catches regressions where the
    bar accidentally becomes three separate bordered boxes."""
    logged_in_page.locator("#search-btn").click()
    bar = logged_in_page.locator("#search-bar")
    expect(bar).to_be_visible()

    # The bar itself has a border + a generous pill-ish radius
    assert has_border(logged_in_page, bar), "search bar must own its border"
    radius = computed_px(logged_in_page, bar, "border-top-left-radius")
    assert radius >= 16, f"search bar should be pill-shaped: radius={radius}"

    # The input must NOT carry its own border (the pill does)
    inp = logged_in_page.locator("#search-input")
    assert not has_border(logged_in_page, inp), (
        "search input must be borderless — the .search-bar carries the border"
    )

    # The close button likewise borderless
    clear = logged_in_page.locator("#search-clear")
    assert not has_border(logged_in_page, clear), (
        "search-clear must be borderless inside the pill"
    )


def test_search_bar_has_no_internal_gaps_breaking_pill(logged_in_page):
    """Children sit inside the pill, not floating with margins that would
    visually separate them."""
    logged_in_page.locator("#search-btn").click()
    for sel in ("#search-input", "#search-clear"):
        loc = logged_in_page.locator(sel)
        # No margins poking outside the pill
        for side in ("top", "right", "bottom", "left"):
            assert computed_px(logged_in_page, loc, f"margin-{side}") == 0, (
                f"{sel} has margin-{side} != 0"
            )


# ---------- §2 section labels ----------

def test_section_labels_are_small_and_dim(logged_in_page):
    """Active/Inactive labels: small (≤14px), font-weight bold-ish, color
    distinct from body text (i.e. dim)."""
    body_color = computed(logged_in_page, logged_in_page.locator("body"), "color")
    for sel in ("#sessions-active .section-label-text",
                "#sessions-inactive .section-label-text"):
        loc = logged_in_page.locator(sel)
        size = computed_px(logged_in_page, loc, "font-size")
        assert size <= 14, f"{sel} font-size {size} > 14"
        color = computed(logged_in_page, loc, "color")
        assert color != body_color, f"{sel} color {color} same as body — not dim"


def test_inactive_chevron_left_of_label(logged_in_page):
    """Spec §2.1: ▶ sits at the start of the Inactive header, not the end."""
    chev = logged_in_page.locator("#sessions-inactive .chevron")
    label = logged_in_page.locator("#sessions-inactive .section-label-text")
    cb = chev.bounding_box()
    lb = label.bounding_box()
    assert cb is not None and lb is not None
    assert cb["x"] + cb["width"] <= lb["x"] + 2, (
        f"chevron should precede label, got chev right={cb['x']+cb['width']} "
        f"label left={lb['x']}"
    )


# ---------- §3 directory modal ----------

def test_modal_has_semi_transparent_backdrop(logged_in_page):
    """Click Browse to open modal; the .modal-bg is a tinted overlay."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.open_new_modal()
    hp.spawn_cwd.fill("/tmp")
    hp.browse_btn.click()
    bg = logged_in_page.locator("#modal-browse")
    expect(bg).to_be_visible()
    # Background-color has some alpha (not fully transparent, not opaque)
    bg_color = computed(logged_in_page, bg, "background-color")
    # Accepts rgba(...,a) where 0 < a < 1
    m = re.match(r"rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\s*\)", bg_color)
    assert m, f"unexpected background-color: {bg_color!r}"
    alpha = float(m.group(4) or "1")
    assert 0 < alpha < 1, f"backdrop alpha {alpha} should be partial"


# ---------- §15 wide ----------

def test_wide_sidebar_has_border(wide_page):
    """Spec §15: 320px sidebar visually separated from chat with a divider."""
    hp = HomePage(wide_page)
    hp.expect_visible()
    sidebar = wide_page.locator("#view-home")
    # Right border or just a divider via border-right
    rb = computed_px(wide_page, sidebar, "border-right-width")
    assert rb >= 1, f"sidebar should have right border, got {rb}"
