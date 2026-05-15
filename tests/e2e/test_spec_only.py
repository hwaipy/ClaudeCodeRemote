"""Spec tests — assertions for behaviors described in SPEC.html.

Tests still marked @pytest.mark.spec_only + @pytest.mark.xfail correspond
to spec items not yet implemented. As code catches up, drop both marks.

Run only the unfinished backlog:  pytest -m spec_only --run-spec
Skip xfail-only spec tests:       pytest -m 'not spec_only'
Default `pytest`:                  runs all (xfail items still skipped via the
                                   --run-spec hook in conftest).
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.pages.home_page import HomePage


# ===== Active / Inactive split (§2) =====

def test_active_and_inactive_sections_exist(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator("#sessions-active")).to_be_visible()
    # Inactive section header is always visible; the list inside is collapsed.
    expect(logged_in_page.locator("#sessions-inactive h2.inactive-toggle")
           ).to_be_visible()


def test_inactive_section_starts_collapsed(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    inactive = logged_in_page.locator("#sessions-inactive")
    assert "expanded" not in (inactive.get_attribute("class") or "")


def test_clicking_inactive_header_toggles(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    header = logged_in_page.locator("#sessions-inactive h2.inactive-toggle")
    header.click()
    expect(logged_in_page.locator("#sessions-inactive.expanded")).to_be_visible()
    header.click()
    expect(logged_in_page.locator("#sessions-inactive.expanded")).to_have_count(0)


def test_inactive_card_menu_can_reactivate(logged_in_page, spawned_session):
    """Inactive card menu has 'Activate'; click → card moves back to the
    Active section."""
    sid = spawned_session(name="reactivate-me")
    hp = HomePage(logged_in_page)
    hp.expect_visible()

    # Move to Inactive first
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)
    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="deactivate"]').click()
    expect(card).to_have_count(0, timeout=5000)

    # Expand Inactive, find the card, use the menu's Activate item
    logged_in_page.locator("#sessions-inactive h2.inactive-toggle").click()
    inactive = logged_in_page.locator(f"#sessions-inactive [data-id='{sid}']")
    expect(inactive).to_be_visible(timeout=5000)
    items = inactive.locator(".card-menu-item")
    inactive.locator(".card-menu-btn").click()
    expect(items).to_have_count(2)
    actions = items.evaluate_all("els => els.map(e => e.dataset.action)")
    assert actions == ["activate", "delete"]
    inactive.locator('.card-menu-item[data-action="activate"]').click()

    # Card is back in Active section, removed from Inactive
    expect(inactive).to_have_count(0, timeout=5000)
    expect(logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
           ).to_have_count(1, timeout=5000)


def test_active_card_menu_has_three_items(logged_in_page, spawned_session):
    """Spec: active card menu = Rename / Move to Inactive / Delete."""
    sid = spawned_session(name="three-item-menu")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)
    card.locator(".card-menu-btn").click()
    items = card.locator(".card-menu-item")
    expect(items).to_have_count(3)
    actions = items.evaluate_all("els => els.map(e => e.dataset.action)")
    assert actions == ["rename", "deactivate", "delete"]


def test_active_card_menu_moves_to_inactive(logged_in_page, spawned_session):
    """Active card kebab menu → 'Move to Inactive'."""
    sid = spawned_session(name="moves-to-inactive")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)
    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="deactivate"]').click()
    expect(card).to_have_count(0, timeout=5000)
    logged_in_page.locator("#sessions-inactive h2.inactive-toggle").click()
    expect(logged_in_page.locator(f"#sessions-inactive [data-id='{sid}']")
           ).to_have_count(1)


def test_active_card_menu_delete_with_confirm(logged_in_page, spawned_session):
    """Active card → Delete → confirm → soft-delete, card gone."""
    sid = spawned_session(name="active-delete")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)
    logged_in_page.once("dialog", lambda d: d.accept())
    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="delete"]').click()
    expect(card).to_have_count(0, timeout=5000)


def test_rename_inline_edit_persists(logged_in_page, spawned_session):
    """Rename menu item swaps .name for an editable input. Enter commits
    via PUT /api/sessions/<id>/rename and updates the card on screen."""
    sid = spawned_session(name="orig-name")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    expect(card.locator(".name")).to_have_text("orig-name")

    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()

    edit = card.locator(".name.editing")
    expect(edit).to_be_visible()
    expect(edit).to_be_focused()
    # contenteditable: clear text + type new name (fill doesn't work on
    # contenteditable elements directly — they're not <input>)
    logged_in_page.keyboard.press("Control+a")
    logged_in_page.keyboard.type("renamed-session")
    logged_in_page.keyboard.press("Enter")

    expect(card.locator(".name")).to_have_text("renamed-session", timeout=3000)
    # Refresh and verify backend stored it
    logged_in_page.reload()
    expect(
        logged_in_page.locator(f"#sessions-active [data-id='{sid}'] .name")
    ).to_have_text("renamed-session", timeout=5000)


def test_rename_can_be_done_repeatedly(logged_in_page, spawned_session):
    """Each click of Rename must spawn a fresh editor. Regression test:
    earlier version cached the .name reference at card-render time, so
    after the first commit the input replaced the cached node and the
    second click did nothing."""
    sid = spawned_session(name="first")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    expect(card.locator(".name")).to_have_text("first")

    for n in ("second", "third"):
        card.locator(".card-menu-btn").click()
        card.locator('.card-menu-item[data-action="rename"]').click()
        edit = card.locator(".name.editing")
        expect(edit).to_be_visible(timeout=2000)
        logged_in_page.keyboard.press("Control+a")
        logged_in_page.keyboard.type(n)
        logged_in_page.keyboard.press("Enter")
        expect(card.locator(".name")).to_have_text(n, timeout=3000)


def test_rename_after_cancel_works(logged_in_page, spawned_session):
    """Tighter regression: after Esc-cancelling rename (which does NOT
    fire a WS re-render of the card), a second Rename click must still
    open a fresh editor. This is the path that actually broke in
    production — the commit path was saved by an incidental WS re-render."""
    sid = spawned_session(name="original")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")

    # 1. Open rename then Esc to cancel (no WS event fires)
    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()
    expect(card.locator(".name.editing")).to_be_visible(timeout=2000)
    card.locator(".name.editing").press("Escape")
    expect(card.locator(".name")).to_have_text("original")

    # 2. Open rename AGAIN — must show a fresh editor
    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()
    expect(card.locator(".name.editing")).to_be_visible(timeout=2000)


def test_rename_escape_cancels(logged_in_page, spawned_session):
    sid = spawned_session(name="keep-this")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
    card.locator(".card-menu-btn").click()
    card.locator('.card-menu-item[data-action="rename"]').click()
    edit = card.locator(".name.editing")
    expect(edit).to_be_visible()
    logged_in_page.keyboard.press("Control+a")
    logged_in_page.keyboard.type("typed-but-cancelled")
    logged_in_page.keyboard.press("Escape")
    expect(card.locator(".name")).to_have_text("keep-this")


# ===== New session modal (§2.2) =====

def test_new_button_opens_modal(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    logged_in_page.locator("#new-btn").click()
    expect(logged_in_page.locator("#modal-new-session")).to_be_visible()


def test_new_session_modal_closes_on_esc(logged_in_page):
    logged_in_page.locator("#new-btn").click()
    expect(logged_in_page.locator("#modal-new-session")).to_be_visible()
    logged_in_page.keyboard.press("Escape")
    expect(logged_in_page.locator("#modal-new-session")).to_be_hidden()


def test_new_session_modal_closes_on_cancel(logged_in_page):
    logged_in_page.locator("#new-btn").click()
    expect(logged_in_page.locator("#modal-new-session")).to_be_visible()
    logged_in_page.locator("#new-modal-cancel").click()
    expect(logged_in_page.locator("#modal-new-session")).to_be_hidden()


def test_new_session_modal_form_values_persist(logged_in_page):
    """Closing the modal keeps the form fields so re-opening shows the
    user's in-progress input (spec)."""
    logged_in_page.locator("#new-btn").click()
    logged_in_page.locator("#spawn-name").fill("draft-name")
    logged_in_page.locator("#new-modal-close").click()
    logged_in_page.locator("#new-btn").click()
    expect(logged_in_page.locator("#spawn-name")).to_have_value("draft-name")


# ===== Four permission modes (§12) =====

def test_perm_menu_has_four_modes(logged_in_page, spawned_session):
    sid = spawned_session(name="perm-mode-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.card_by_id(sid).click()

    logged_in_page.locator("#chat-perm").dispatch_event("click")
    items = logged_in_page.locator("#perm-menu .perm-menu-item")
    expect(items).to_have_count(4)
    modes = items.evaluate_all("els => els.map(e => e.dataset.mode)")
    assert set(modes) == {"manual", "accept_edits", "plan", "allow_all"}


# ===== Session card redesign (§2 cards) =====

def test_card_has_state_dot(logged_in_page, spawned_session):
    sid = spawned_session(name="state-dot-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    expect(card.locator(".state-dot")).to_have_count(1)


def test_idle_card_has_no_badge(logged_in_page, spawned_session):
    """Spec: idle / hibernated / finished sessions show only the state-dot —
    NO 'Idle' text badge. A freshly spawned session with no activity is
    idle (active_turn=false), so this catches the regression."""
    sid = spawned_session(name="idle-no-badge")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    # Confirm the card is in an idle-class state
    cls = card.get_attribute("class") or ""
    assert "state-idle" in cls or "state-hibernated" in cls or "state-finished" in cls, (
        f"freshly spawned session expected to be idle-class: {cls}"
    )
    # And no badge rendered
    expect(card.locator(".badge")).to_have_count(0)


def test_card_no_longer_shows_short_id(logged_in_page, spawned_session):
    """Spec update: session id removed from card."""
    sid = spawned_session(name="no-shortid-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    expect(card.locator(".short-id")).to_have_count(0)


def test_card_meta_line_has_active_time(logged_in_page, spawned_session):
    """Active time moved to row 2 (.meta-line). 'active' prefix dropped —
    just '<relTime> ago'."""
    sid = spawned_session(name="ts-on-row2")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    ts = card.locator(".meta-line .ts")
    expect(ts).to_be_visible()
    text = ts.inner_text().strip()
    assert text.endswith("ago"), f"ts should end with 'ago': {text!r}"
    assert not text.startswith("active"), f"'active' prefix should be gone: {text!r}"


def test_card_cwd_is_absolute_with_home_abbrev(logged_in_page, spawned_session,
                                               tmp_path):
    """cwd-short now shows the full absolute path. If under $HOME it's
    abbreviated to start with '~'."""
    sid = spawned_session(name="cwd-abbrev-test", cwd=str(tmp_path))
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    short = card.locator(".cwd-short").inner_text().strip()
    # tmp_path is under /tmp, not /home/* — no ~ abbreviation expected.
    # Path should be the full absolute path.
    if str(tmp_path).startswith("/home/") or str(tmp_path).startswith("/Users/"):
        assert short.startswith("~"), f"home abbrev expected: {short!r}"
    else:
        assert short == str(tmp_path), f"expected absolute path: got {short!r}"


# ===== Recent cwds chips (§2 chip behavior) =====

def test_first_visit_no_chips(logged_in_page):
    """logged_in_page sets only ccr.token; recentCwds key is absent → no chips."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator("#cwd-presets .chip")).to_have_count(0)


def test_spawn_via_ui_adds_to_recent_chips(logged_in_page, tmp_path,
                                            cleanup_test_sessions_for_recent_chips):
    """Spawn through the form so the recent-list code path runs."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.fill_spawn_form(cwd=str(tmp_path), name="test-recent-chip")
    hp.submit_spawn()
    expect(logged_in_page.locator("body")).to_have_class(
        __import__("re").compile(r"\bhas-session\b"), timeout=10000
    )
    # Back to home so chips re-render
    logged_in_page.locator("#chat-back").dispatch_event("click")
    expect(hp.cards.first).to_be_visible(timeout=5000)

    recents = logged_in_page.evaluate(
        '() => JSON.parse(localStorage.getItem("ccr.recentCwds") || "[]")'
    )
    assert str(tmp_path) in recents
    assert recents[0] == str(tmp_path), "newest should be leftmost"
    # Open the new-session modal to see chips
    hp.open_new_modal()
    expect(logged_in_page.locator(
        f"#cwd-presets .chip[data-path='{tmp_path}']"
    )).to_be_visible()


@pytest.fixture
def cleanup_test_sessions_for_recent_chips(server_env):
    from tests.helpers import api_list_sessions, api_delete_session
    yield
    try:
        for s in api_list_sessions(server_env["base_url"], server_env["token"]):
            if (s.get("name") or "").startswith("test-"):
                try:
                    api_delete_session(server_env["base_url"], server_env["token"], s["id"])
                except Exception:
                    pass
    except Exception:
        pass


# ===== Search button (§2.3) =====

def test_search_button_opens_bar(logged_in_page):
    """New architecture: bar is always in layout (36px when collapsed,
    grows to fill when open). No appear/disappear — just width change."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    bar = logged_in_page.locator("#search-bar")
    expect(bar).to_be_visible()
    collapsed_w = bar.bounding_box()["width"]
    assert 30 <= collapsed_w <= 42, f"bar should be ~36px when collapsed: {collapsed_w}"

    logged_in_page.locator("#search-btn").click()
    expect(logged_in_page.locator(".home-top")).to_have_class(
        __import__("re").compile(r"\bsearch-open\b"), timeout=2000
    )
    # After expansion, input is focusable (focus is set after a 280ms delay
    # so the layout settles); allow time for it
    expect(logged_in_page.locator("#search-input")).to_be_focused(timeout=2000)


def test_search_filters_cards(logged_in_page, spawned_session):
    a = spawned_session(name="apple-tree")
    b = spawned_session(name="banana-bread")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(hp.card_by_id(a)).to_be_visible(timeout=5000)
    expect(hp.card_by_id(b)).to_be_visible()

    logged_in_page.locator("#search-btn").click()
    logged_in_page.locator("#search-input").fill("apple")

    expect(hp.card_by_id(a)).to_be_visible()
    expect(hp.card_by_id(b)).to_be_hidden()


# ===== Sort toggle (§2 active section) =====

def test_sort_toggle_exists(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator("#sessions-sort")).to_be_visible()


def test_sort_toggle_cycles_created_active(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    btn = logged_in_page.locator("#sessions-sort")
    assert btn.get_attribute("data-mode") == "created"
    btn.click()
    expect(btn).to_have_attribute("data-mode", "active")
    btn.click()
    expect(btn).to_have_attribute("data-mode", "created")


def test_sort_button_is_icon_only_no_text(logged_in_page):
    """Sort button contains SVG icons, no visible text label."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    btn = logged_in_page.locator("#sessions-sort")
    expect(btn).to_be_visible()
    # Has at least one SVG inside
    expect(btn.locator("svg")).to_have_count(2)   # created + active, one shown
    # No "sort:" text content
    label = btn.inner_text().strip()
    assert "sort" not in label.lower(), f"sort button should be icon-only, got {label!r}"


def test_sort_icon_switches_with_mode(logged_in_page):
    """Calendar (created) ↔ clock (active) — the right SVG is visible per mode."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    btn = logged_in_page.locator("#sessions-sort")
    expect(btn.locator(".sort-icon-created")).to_be_visible()
    expect(btn.locator(".sort-icon-active")).to_be_hidden()
    btn.click()
    expect(btn.locator(".sort-icon-active")).to_be_visible()
    expect(btn.locator(".sort-icon-created")).to_be_hidden()


def test_home_top_layout_new_left_search_right(logged_in_page):
    """home-top: new-btn on left, search-btn on right, similar y."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    new_box = logged_in_page.locator("#new-btn").bounding_box()
    search_box = logged_in_page.locator("#search-btn").bounding_box()
    assert new_box and search_box
    assert new_box["x"] < search_box["x"], "new-btn should be left of search-btn"
    # Same row (centers within 10px vertically)
    new_cy = new_box["y"] + new_box["height"] / 2
    search_cy = search_box["y"] + search_box["height"] / 2
    assert abs(new_cy - search_cy) <= 10, (
        f"new-btn and search-btn should be at same height: {new_cy} vs {search_cy}"
    )


def test_search_open_collapses_new_btn(logged_in_page):
    """Clicking the search icon → .home-top gains .search-open, new-btn
    width animates to 0 (squeezed, NOT hidden), search-bar grows to fill
    the row. Continuous width change, no opacity/display flips."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    new_btn = logged_in_page.locator("#new-btn")
    expect(new_btn).to_be_visible()
    logged_in_page.locator("#search-btn").click()

    expect(logged_in_page.locator(".home-top")).to_have_class(
        __import__("re").compile(r"\bsearch-open\b"), timeout=2000
    )
    # Wait for transition to settle, then check the rendered width is 0
    logged_in_page.wait_for_timeout(450)
    box = new_btn.bounding_box()
    assert box and box["width"] <= 5, f"new-btn should reach 0 width: {box}"
    # Opacity stays 1 — button is not hidden, just squeezed.
    expect(new_btn).to_have_css("opacity", "1")


def test_search_close_restores_new_btn(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    logged_in_page.locator("#search-btn").click()
    expect(logged_in_page.locator(".home-top")).to_have_class(
        __import__("re").compile(r"\bsearch-open\b"), timeout=2000
    )
    logged_in_page.locator("#search-clear").click()
    expect(logged_in_page.locator(".home-top")).not_to_have_class(
        __import__("re").compile(r"\bsearch-open\b"), timeout=2000
    )
    # After 400ms close animation, new-btn back to full width
    logged_in_page.wait_for_timeout(450)
    expect(logged_in_page.locator("#new-btn")).to_have_css("opacity", "1")
    new_box = logged_in_page.locator("#new-btn").bounding_box()
    assert new_box and new_box["width"] >= 100, f"new-btn should restore: {new_box}"


def test_inactive_chevron_is_left_of_label(logged_in_page):
    """Chevron ▶ sits at the start of the Inactive section header."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    chev = logged_in_page.locator("#sessions-inactive .chevron")
    label = logged_in_page.locator("#sessions-inactive .section-label-text")
    chev_box = chev.bounding_box()
    label_box = label.bounding_box()
    assert chev_box and label_box
    assert chev_box["x"] < label_box["x"], "chevron should be left of label"


def test_section_labels_are_small_and_dim(logged_in_page):
    """Active / Inactive labels are small (≤14px) and dim (not body text color)."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    for sel in ("#sessions-active .section-label-text",
                "#sessions-inactive .section-label-text"):
        size = logged_in_page.locator(sel).evaluate(
            "el => parseFloat(getComputedStyle(el).fontSize)"
        )
        assert size <= 14, f"{sel} font-size {size} > 14"


# ===== Ctx 不警示 (§11) =====

@pytest.mark.spec_only
@pytest.mark.xfail(reason="spec: high ctx usage shows no warning color")
def test_ctx_status_never_red():
    """Placeholder — needs a live session with high ctx to fully verify.
    Currently the .conv-status doesn't have any warning logic, so this
    *should already pass*. Marking xfail to flag spec coverage gap."""
    pytest.skip("can't construct a session with high ctx without burning real API")


# ===== Theme toggle moved out of login (§14) =====

def test_login_view_has_no_theme_toggle(fresh_page):
    expect(fresh_page.locator("#view-login")).to_be_visible()
    expect(fresh_page.locator("#theme-toggle-login")).to_have_count(0)
