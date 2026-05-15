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

def test_home_footer_is_at_bottom_dim_and_small(logged_in_page):
    """Spec §2: Sign out / Hard reload pinned to view-home bottom, dim+tiny."""
    footer = logged_in_page.locator(".home-footer")
    expect(footer).to_be_visible()
    # Small font
    size = computed_px(logged_in_page, footer, "font-size")
    assert size <= 13, f"home-footer font-size {size} too big"
    # Dim (opacity < 1 or very muted color — opacity is the spec)
    op = float(computed(logged_in_page, footer, "opacity") or "1")
    assert op <= 0.7, f"home-footer opacity {op} too prominent"
    # Sits below the session list area
    list_box = logged_in_page.locator("#sessions-active").bounding_box()
    foot_box = footer.bounding_box()
    assert list_box and foot_box
    assert foot_box["y"] > list_box["y"], "footer should be below session list"


def test_home_has_no_outer_frame(logged_in_page):
    """Spec §2: home content sits directly on the page bg, no card frame."""
    card = logged_in_page.locator("#view-home .card")
    expect(card).to_be_visible()
    assert not has_border(logged_in_page, card), "home .card must have no border"
    bg = computed(logged_in_page, card, "background-color")
    assert bg in ("rgba(0, 0, 0, 0)", "transparent"), (
        f"home .card background must be transparent, got {bg!r}"
    )


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


def test_search_bar_animates_open(logged_in_page):
    """Spec §2.1: search expansion is animated, not a snap."""
    bar = logged_in_page.locator("#search-bar")
    transition = computed(logged_in_page, bar, "transition")
    # Has some animated property (width / opacity / transform / padding)
    assert any(p in transition for p in ("width", "transform", "opacity", "padding")), (
        f"#search-bar should declare a transition: {transition!r}"
    )
    # Non-zero duration somewhere in the declaration
    m = re.search(r"([0-9.]+)\s*(ms|s)\b", transition)
    assert m, f"no animation duration in {transition!r}"
    secs = float(m.group(1)) / (1000.0 if m.group(2) == "ms" else 1.0)
    assert 0.1 <= secs <= 1.0, f"animation duration {secs}s out of expected range"


def test_search_close_is_staged(logged_in_page):
    """Close direction is the mirror of open:
      1. Children fade out first
      2. Bar visibly shrinks rightward (with its own delay so it doesn't
         race with the fade)
      3. Home-top fades back in only after the bar is fully collapsed
    Encoded via transition-delay on .search-bar (close-direction width
    delay) and on .home-top (close-direction fade-in delay)."""
    bar = logged_in_page.locator("#search-bar")
    home_top = logged_in_page.locator(".home-top")

    def first_delay(loc):
        raw = computed(logged_in_page, loc, "transition-delay")
        m = re.search(r"(-?[0-9.]+)\s*(ms|s)", raw)
        assert m, f"no delay declared: {raw!r}"
        return float(m.group(1)) / (1000.0 if m.group(2) == "ms" else 1.0)

    assert first_delay(bar) >= 0.05, "bar shrink delay too short for staged close"
    assert first_delay(home_top) >= 0.25, "home-top fade-in delay too short"


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

def test_active_and_inactive_labels_match(logged_in_page):
    """Spec: Active and Inactive section labels share font + vertical
    spacing so they read as the same kind of header."""
    a = logged_in_page.locator("#sessions-active .section-label")
    i = logged_in_page.locator("#sessions-inactive .section-label")
    for prop in ("font-size", "font-weight", "text-transform", "letter-spacing",
                 "color", "margin-top", "margin-bottom"):
        va = computed(logged_in_page, a, prop)
        vi = computed(logged_in_page, i, prop)
        assert va == vi, f"{prop} mismatch: active={va!r} inactive={vi!r}"


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
