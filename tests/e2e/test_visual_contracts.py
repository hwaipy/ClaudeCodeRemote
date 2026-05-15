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

def test_home_footer_sticks_to_bottom_when_content_short(logged_in_page):
    """Sticky footer behaviour: with little content, .home-footer is
    pushed to the bottom of the scroll container by margin-top:auto, so
    visually it sits near the viewport edge. NOT position:fixed."""
    footer = logged_in_page.locator(".home-footer")
    wrap = logged_in_page.locator("#view-home > .center-wrap")
    fb = footer.bounding_box()
    wb = wrap.bounding_box()
    assert fb and wb
    # Footer's bottom should be tight against .center-wrap's bottom
    distance = (wb["y"] + wb["height"]) - (fb["y"] + fb["height"])
    assert 0 <= distance <= 8, (
        f"footer should sit at .center-wrap bottom: footer_bottom={fb['y']+fb['height']}, "
        f"wrap_bottom={wb['y']+wb['height']}, gap={distance}"
    )
    pos = computed(logged_in_page, footer, "position")
    assert pos != "fixed", f"footer must not be fixed: {pos}"
    assert logged_in_page.locator(
        "#view-home > .center-wrap > .home-footer"
    ).count() == 1, "footer should be a direct child of .center-wrap"


def test_home_footer_is_subtle(logged_in_page):
    """Spec: footer should be as inconspicuous as possible — tiny, dim."""
    footer = logged_in_page.locator(".home-footer")
    size = computed_px(logged_in_page, footer, "font-size")
    assert size <= 11, f"footer font-size {size} too large"
    op = float(computed(logged_in_page, footer, "opacity") or "1")
    assert op <= 0.5, f"footer opacity {op} too prominent"


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


def test_long_cwd_truncates_to_preserve_active_time(logged_in_page,
                                                    spawned_session, tmp_path):
    """Spec: when row 2 is tight, cwd-short truncates with ellipsis BUT
    the 'active X ago' span stays whole. ts has flex: 0 0 auto. Spawn a
    session in a deliberately long path to force tightness."""
    long_dir = tmp_path / ("a" * 30) / ("b" * 30)
    long_dir.mkdir(parents=True)
    sid = spawned_session(name="cwd-overflow", cwd=str(long_dir))
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)

    cwd = card.locator(".cwd-short")
    ts = card.locator(".ts")
    cwd_box = cwd.bounding_box()
    ts_box = ts.bounding_box()
    card_box = card.bounding_box()
    assert cwd_box and ts_box and card_box

    ts_right = ts_box["x"] + ts_box["width"]
    card_right = card_box["x"] + card_box["width"]
    assert ts_right <= card_right - 4, (
        f"active-time should fit fully: ts_right={ts_right} card_right={card_right}"
    )
    overflow = computed(logged_in_page, cwd, "overflow")
    text_overflow = computed(logged_in_page, cwd, "text-overflow")
    assert overflow == "hidden", f"cwd-short overflow={overflow!r}"
    assert text_overflow == "ellipsis", f"cwd-short text-overflow={text_overflow!r}"


def test_ts_right_aligned_in_meta_line(logged_in_page, spawned_session):
    """ts should sit at the right edge of the .meta-line. Concretely:
    its right edge should be (close to) the meta-line's right edge."""
    sid = spawned_session(name="ts-right-align")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    line = card.locator(".meta-line")
    ts = card.locator(".ts")
    lb = line.bounding_box()
    tb = ts.bounding_box()
    assert lb and tb
    drift = (lb["x"] + lb["width"]) - (tb["x"] + tb["width"])
    assert 0 <= drift <= 2, (
        f"ts should hug meta-line right edge: drift={drift}px"
    )


def test_truncated_name_gets_title_tooltip(logged_in_page, spawned_session):
    """Spec: truncated .name carries the full text in a title= tooltip."""
    full = "this-is-a-really-long-session-name-that-must-be-truncated"
    sid = spawned_session(name=full)
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    name = card.locator(".name")
    expect(name).to_have_attribute("title", full, timeout=2000)


def test_short_name_has_no_title_tooltip(logged_in_page, spawned_session):
    """Spec: a name that fits in the card has NO title (no useless tooltip
    showing the same thing the user already sees)."""
    sid = spawned_session(name="ab")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    logged_in_page.wait_for_timeout(100)   # let rAF settle
    title = card.locator(".name").get_attribute("title")
    assert not title, f"short name should have no tooltip: {title!r}"


def test_truncated_cwd_gets_title_tooltip(logged_in_page, spawned_session, tmp_path):
    """Spec: truncated .cwd-short carries the full path in a title= tooltip."""
    long_dir = tmp_path / ("aaaa" * 10) / ("bbbb" * 10)
    long_dir.mkdir(parents=True)
    sid = spawned_session(name="cwd-tip-test", cwd=str(long_dir))
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    cwd = card.locator(".cwd-short")
    # title should be the full cwd path (not abbreviated)
    expect(cwd).to_have_attribute("title", str(long_dir), timeout=2000)


def test_long_name_doesnt_run_under_kebab(logged_in_page, spawned_session):
    """Truncated long names must end with a visible gap before the kebab,
    not run beneath it. .session-row1 reserves right-padding for that."""
    long_name = "this-is-a-really-long-session-name-that-must-be-truncated"
    sid = spawned_session(name=long_name)
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)

    name_box = card.locator(".name").bounding_box()
    kebab_box = card.locator(".card-menu-btn").bounding_box()
    assert name_box and kebab_box

    # Right edge of name must be left of kebab's left edge with ≥8px gap
    gap = kebab_box["x"] - (name_box["x"] + name_box["width"])
    assert gap >= 8, (
        f"name should end before kebab with breathing room: "
        f"name_right={name_box['x']+name_box['width']}, "
        f"kebab_left={kebab_box['x']}, gap={gap}px"
    )


def test_rename_to_long_name_keeps_card_layout(logged_in_page, spawned_session):
    """Spec: after renaming to a long string the .name should keep its
    flex:1 share of row 1 (ellipsizes long content). Its rendered width
    must not collapse to a sliver."""
    sid = spawned_session(name="short")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    name_w_before = card.locator(".name").bounding_box()["width"]
    assert name_w_before > 40

    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()
    expect(card.locator(".name.editing")).to_be_visible(timeout=2000)
    logged_in_page.keyboard.press("Control+a")
    logged_in_page.keyboard.type("a" * 80)
    logged_in_page.keyboard.press("Enter")
    logged_in_page.wait_for_timeout(800)   # past renameInFlight grace

    name_w_after = card.locator(".name").bounding_box()["width"]
    assert abs(name_w_before - name_w_after) <= 5, (
        f"name width collapsed after long rename: "
        f"before={name_w_before}, after={name_w_after}"
    )


def test_rename_no_blank_period_after_commit(logged_in_page, spawned_session):
    """Spec: after Enter, .name must continuously show non-empty text —
    no destroy-and-rebuild blank moment during the WS-echo re-render."""
    sid = spawned_session(name="orig")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()
    edit = card.locator(".name.editing")
    expect(edit).to_be_visible()
    logged_in_page.keyboard.press("Control+a")
    logged_in_page.keyboard.type("renamed-no-flash")
    logged_in_page.keyboard.press("Enter")

    # Sample text content at multiple times; must never be blank
    samples = []
    for delta in (20, 30, 40, 60, 90, 150, 300, 600):
        logged_in_page.wait_for_timeout(delta)
        text = card.locator(".name").text_content() or ""
        samples.append(text.strip())
    for i, t in enumerate(samples):
        assert t, f"name went blank at sample {i}: samples={samples}"
    # Final state shows the new name
    assert samples[-1] == "renamed-no-flash", samples


def test_rename_editor_shows_full_original_name(logged_in_page, spawned_session):
    """Spec: entering rename on a truncated name reveals the FULL original
    text — no leaked '…' from the displayed ellipsis state, and the
    text-overflow CSS switches to clip so no visual ellipsis is drawn."""
    full = "this-is-a-very-long-original-session-name-for-edit-test"
    sid = spawned_session(name=full)
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()
    name_el = card.locator(".name.editing")
    expect(name_el).to_be_visible(timeout=2000)
    # DOM text content must equal the full original
    text = name_el.evaluate("el => el.textContent")
    assert text == full, f"editor content mismatch: got {text!r}, want {full!r}"
    # No '…' character in the DOM text (sanity)
    assert "…" not in text and "..." not in text
    # CSS text-overflow disabled in edit mode → no visual ellipsis
    text_overflow = computed(logged_in_page, name_el, "text-overflow")
    assert text_overflow == "clip", f"text-overflow={text_overflow!r}"


def test_rename_long_name_stays_in_card(logged_in_page, spawned_session):
    """Spec: entering rename on a long name must NOT make the .name
    element overflow the card boundary."""
    long_name = "a" * 60
    sid = spawned_session(name=long_name)
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    card_box = card.bounding_box()

    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()
    expect(card.locator(".name.editing")).to_be_visible(timeout=2000)

    name_box = card.locator(".name").bounding_box()
    card_right = card_box["x"] + card_box["width"]
    name_right = name_box["x"] + name_box["width"]
    # Name's right edge must stay within the card (allow a 4px outline halo)
    assert name_right <= card_right + 4, (
        f"name overflows card: name_right={name_right}, card_right={card_right}"
    )


def test_rename_doesnt_shift_card_layout(logged_in_page, spawned_session):
    """Entering and leaving the rename editor must not change the card's
    rendered bounding box. The .name-edit input has to occupy the exact
    same footprint as the .name div it replaces."""
    sid = spawned_session(name="layout-stable")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)

    box_before = card.bounding_box()

    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()
    expect(card.locator(".name.editing")).to_be_visible(timeout=2000)
    box_editing = card.bounding_box()

    card.locator(".name.editing").press("Escape")
    expect(card.locator(".name")).to_be_visible(timeout=2000)
    box_after = card.bounding_box()

    for prop in ("width", "height"):
        for label, b in (("editing", box_editing), ("after", box_after)):
            diff = abs(box_before[prop] - b[prop])
            assert diff <= 1, (
                f"card {prop} shifted on {label}: "
                f"before={box_before[prop]} {label}={b[prop]} diff={diff}"
            )


def test_session_card_kebab_visible_by_default(logged_in_page, spawned_session):
    """Spec: kebab ⋯ menu button lives at the card's top-right and is
    visible all the time (no hover required)."""
    sid = spawned_session(name="kebab-visible-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    btn = card.locator(".card-menu-btn")
    expect(btn).to_be_visible()


def test_session_card_kebab_anchored_top_right(logged_in_page, spawned_session):
    """Kebab sits in the card's top-right corner (≤14px inset)."""
    sid = spawned_session(name="kebab-pos-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    btn = card.locator(".card-menu-btn")
    cb = card.bounding_box()
    bb = btn.bounding_box()
    assert cb and bb
    right_inset = (cb["x"] + cb["width"]) - (bb["x"] + bb["width"])
    top_inset = bb["y"] - cb["y"]
    assert 0 <= right_inset <= 14, f"kebab right inset {right_inset}"
    assert 0 <= top_inset <= 14, f"kebab top inset {top_inset}"


def test_clicking_kebab_opens_menu(logged_in_page, spawned_session):
    sid = spawned_session(name="kebab-open-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    menu = card.locator(".card-menu")
    expect(menu).to_be_hidden()
    card.locator(".card-menu-btn").click()
    expect(menu).to_be_visible()
    expect(card.locator('.card-menu-item[data-action="deactivate"]')).to_be_visible()


def test_clicking_outside_menu_closes_it(logged_in_page, spawned_session):
    sid = spawned_session(name="kebab-close-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    card.locator(".card-menu-btn").click()
    menu = card.locator(".card-menu")
    expect(menu).to_be_visible()
    # Click on the section label outside the menu
    logged_in_page.locator("#sessions-active .section-label-text").click()
    expect(menu).to_be_hidden()


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


def test_search_bar_is_round_when_collapsed(logged_in_page):
    """Spec §2.1: when collapsed the whole search-bar is a round chip.
    There's no separate round button now — clicking the bar (or the icon
    inside) opens it."""
    bar = logged_in_page.locator("#search-bar")
    box = bar.bounding_box()
    assert box is not None
    assert abs(box["width"] - box["height"]) <= 6
    radius = computed_px(logged_in_page, bar, "border-top-left-radius")
    assert radius >= box["width"] / 2 - 4, (
        f"search-bar not round enough when collapsed: radius={radius}, w={box['width']}"
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
    icon / input / close inside borderless."""
    logged_in_page.locator("#search-btn").click()
    bar = logged_in_page.locator("#search-bar")
    logged_in_page.wait_for_timeout(450)  # let expansion settle
    expect(bar).to_be_visible()

    assert has_border(logged_in_page, bar), "search bar must own its border"
    radius = computed_px(logged_in_page, bar, "border-top-left-radius")
    assert radius >= 16, f"search bar should be pill-shaped: radius={radius}"

    inp = logged_in_page.locator("#search-input")
    assert not has_border(logged_in_page, inp), (
        "search input must be borderless — the .search-bar carries the border"
    )
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


def test_search_button_no_focus_ring(logged_in_page):
    """Spec: the search lens should feel like an icon, not a button.
    No outline on focus, no press-darkening on :active."""
    btn = logged_in_page.locator("#search-btn")
    # Force focus
    btn.focus()
    outline_w = computed(logged_in_page, btn, "outline-width")
    outline_s = computed(logged_in_page, btn, "outline-style")
    assert outline_w in ("0px", "") or outline_s in ("none", ""), (
        f"search-btn should have no focus ring: outline={outline_w} {outline_s}"
    )
    # No webkit tap highlight either (matters on iOS)
    tap = computed(logged_in_page, btn, "-webkit-tap-highlight-color")
    if tap:
        assert "0)" in tap or tap == "transparent", (
            f"-webkit-tap-highlight-color should be transparent: {tap!r}"
        )


def test_new_btn_visibly_squeezes_on_search_open(logged_in_page):
    """Playback test: clicking search must visibly squeeze new-btn from
    its natural width down to 0, NOT just snap it away.

    The implementation measures the button's current width inline before
    adding .search-open so the transition has concrete pixel endpoints.
    A 170ms sample must land in the middle of that animation, NOT at
    either extreme — that's the contract that catches "snap" regressions."""
    new_btn = logged_in_page.locator("#new-btn")
    w_start = new_btn.bounding_box()["width"]
    assert w_start >= 80, f"new-btn should have natural width: {w_start}"

    logged_in_page.locator("#search-btn").click()
    logged_in_page.wait_for_timeout(170)
    w_mid = new_btn.bounding_box()["width"]
    assert 5 < w_mid < w_start - 20, (
        f"new-btn must be visibly mid-squeeze 170ms after search opens: "
        f"start={w_start}, mid={w_mid} (snap?)"
    )

    logged_in_page.wait_for_timeout(450)
    w_end = new_btn.bounding_box()["width"]
    assert w_end <= 5, f"new-btn should reach 0 width once open: {w_end}"


def test_new_btn_visibly_un_squeezes_on_search_close(logged_in_page):
    """Playback test: closing search must visibly expand new-btn from 0
    back to its natural width."""
    new_btn = logged_in_page.locator("#new-btn")
    w_natural = new_btn.bounding_box()["width"]
    logged_in_page.locator("#search-btn").click()
    logged_in_page.wait_for_timeout(450)
    assert new_btn.bounding_box()["width"] <= 5, "new-btn should be collapsed"

    logged_in_page.locator("#search-clear").click()
    logged_in_page.wait_for_timeout(170)
    w_mid = new_btn.bounding_box()["width"]
    assert 5 < w_mid < w_natural - 20, (
        f"new-btn must be visibly mid-expand 170ms after close: "
        f"natural={w_natural}, mid={w_mid}"
    )


def test_search_bar_right_edge_stable_during_close(logged_in_page):
    """The bar is anchored to the row's right edge. During close, its
    LEFT edge slides rightward (width shrinks) but its RIGHT edge must
    stay put — any wobble = layout overflow / flex snap regression."""
    bar = logged_in_page.locator("#search-bar")
    logged_in_page.locator("#search-btn").click()
    logged_in_page.wait_for_timeout(450)
    box = bar.bounding_box()
    right_open = box["x"] + box["width"]

    logged_in_page.locator("#search-clear").click()
    samples = []
    for step in (30, 60, 80, 80, 80, 80, 80):
        logged_in_page.wait_for_timeout(step)
        b = bar.bounding_box()
        samples.append(b["x"] + b["width"])
    drift = [abs(r - right_open) for r in samples]
    # Allow 1.5px sub-pixel rounding tolerance
    assert max(drift) <= 1.5, (
        f"search-bar right edge drifted during close: open={right_open}, "
        f"samples={samples}, max drift={max(drift)}px"
    )


def test_search_bar_right_edge_stable_during_open(logged_in_page):
    """Mirror: during open the right edge must stay put while only the
    left edge moves leftward."""
    bar = logged_in_page.locator("#search-bar")
    box = bar.bounding_box()
    right_initial = box["x"] + box["width"]

    logged_in_page.locator("#search-btn").click()
    samples = []
    for step in (30, 60, 80, 80, 80, 80, 80):
        logged_in_page.wait_for_timeout(step)
        b = bar.bounding_box()
        samples.append(b["x"] + b["width"])
    drift = [abs(r - right_initial) for r in samples]
    assert max(drift) <= 1.5, (
        f"search-bar right edge drifted during open: initial={right_initial}, "
        f"samples={samples}, max drift={max(drift)}px"
    )


def test_new_btn_width_grows_monotonically_during_close(logged_in_page):
    """Stronger smoothness check: sample new-btn width 4× during the
    close transition and assert each sample is ≥ the previous. Catches
    'jitter' regressions where width oscillates or backsteps mid-animation."""
    new_btn = logged_in_page.locator("#new-btn")
    # Open and let it settle
    logged_in_page.locator("#search-btn").click()
    logged_in_page.wait_for_timeout(450)

    logged_in_page.locator("#search-clear").click()
    samples = []
    for step in (60, 60, 60, 100, 100):
        logged_in_page.wait_for_timeout(step)
        samples.append(new_btn.bounding_box()["width"])
    for i in range(1, len(samples)):
        assert samples[i] + 1.5 >= samples[i - 1], (
            f"new-btn width should grow monotonically during close: {samples}"
        )
    assert samples[-1] >= samples[0] + 30, (
        f"close should have made visible progress: {samples}"
    )


def test_search_input_focused_immediately_on_open(logged_in_page):
    """Spec: clicking search focuses the input synchronously (no setTimeout)
    so iOS Safari honours the user gesture and pops the keyboard."""
    logged_in_page.locator("#search-btn").click()
    expect(logged_in_page.locator("#search-input")).to_be_focused(timeout=200)


def test_click_outside_bar_auto_closes(logged_in_page):
    """Spec: clicking outside the search bar while it's open auto-closes it."""
    import re as _re
    logged_in_page.locator("#search-btn").click()
    expect(logged_in_page.locator(".home-top")).to_have_class(
        _re.compile(r"\bsearch-open\b")
    )
    # Click somewhere outside the bar — the section label is harmless
    logged_in_page.locator("#sessions-active .section-label-text").click()
    expect(logged_in_page.locator(".home-top")).not_to_have_class(
        _re.compile(r"\bsearch-open\b"), timeout=1000
    )


def test_search_close_actually_animates_visibly(logged_in_page):
    """Playback test (not just CSS declaration): sample bar width at
    multiple times during close and assert it smoothly decreases.

    With the flex-row architecture both sides resize: the bar shrinks
    from ~full-width to 36px (collapsed icon size). Width at the midpoint
    must be strictly between these two extremes — otherwise the close
    snapped without animation."""
    logged_in_page.locator("#search-btn").click()
    bar = logged_in_page.locator("#search-bar")
    logged_in_page.wait_for_timeout(450)   # let open complete
    w_open = bar.bounding_box()["width"]
    assert w_open >= 200, f"bar should be wide when open: {w_open}"

    logged_in_page.locator("#search-clear").click()
    logged_in_page.wait_for_timeout(170)
    w_mid = bar.bounding_box()["width"]
    # Between the collapsed 36px target and the open width
    assert 40 < w_mid < w_open - 20, (
        f"bar should be visibly mid-collapse 170ms after close: "
        f"open={w_open}, mid={w_mid} (snap?)"
    )

    logged_in_page.wait_for_timeout(600)
    w_end = bar.bounding_box()["width"]
    assert 30 <= w_end <= 42, f"bar should settle to collapsed icon size: {w_end}"


def test_search_close_is_staged(logged_in_page):
    """Close visual sequence (flex-row architecture):
      - children (input/clear) fade out fast (~150ms)
      - search-bar width transitions 100% → 36px over ~350ms
      - new-btn max-width transitions 0 → 100% in parallel
    Both bar and new-btn use the SAME transition spec in both states
    (no per-state override) so iOS Safari handles them identically."""
    bar = logged_in_page.locator("#search-bar")
    new_btn = logged_in_page.locator("#new-btn")
    inp = logged_in_page.locator("#search-input")

    def first_duration(loc, prop):
        raw = computed(logged_in_page, loc, prop)
        m = re.search(r"(-?[0-9.]+)\s*(ms|s)", raw)
        assert m, f"no value declared for {prop}: {raw!r}"
        return float(m.group(1)) / (1000.0 if m.group(2) == "ms" else 1.0)

    bar_dur = first_duration(bar, "transition-duration")
    new_btn_dur = first_duration(new_btn, "transition-duration")
    input_dur = first_duration(inp, "transition-duration")
    # Bar and new-btn must take similar time — they animate together
    assert abs(bar_dur - new_btn_dur) < 0.05, (
        f"bar ({bar_dur}s) and new-btn ({new_btn_dur}s) should animate in sync"
    )
    # Input fades out faster than the bar shrinks so the empty collapse is visible
    assert input_dur + 0.1 < bar_dur, (
        f"input fade ({input_dur}s) must finish before bar collapse ({bar_dur}s)"
    )


def test_search_bar_has_no_internal_gaps_breaking_pill(logged_in_page):
    """Children sit inside the pill — no large margins that would visibly
    separate them or push past the rounded edges. Small insets (≤6px) are
    fine for keeping content off the rounded ends."""
    logged_in_page.locator("#search-btn").click()
    logged_in_page.wait_for_timeout(450)
    for sel in ("#search-input", "#search-clear"):
        loc = logged_in_page.locator(sel)
        for side in ("top", "right", "bottom", "left"):
            m = computed_px(logged_in_page, loc, f"margin-{side}")
            assert m <= 6, f"{sel} has margin-{side}={m}px, too big for inside-pill inset"


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
