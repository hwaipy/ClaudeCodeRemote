"""Spec-only tests — encode behaviors that are in SPEC.html but not yet in code.

Every test here is xfail. As the implementation catches up, remove the xfail
mark and the test starts gating the feature.

Run only spec backlog:    pytest -m spec_only
Run everything else:      pytest -m 'not spec_only'
Default run:              pytest    (these show as xfail/xpassed, suite stays green)
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.pages.home_page import HomePage

pytestmark = [pytest.mark.spec_only]


# ===== Active / Inactive split (§2) =====

@pytest.mark.xfail(reason="spec: Active/Inactive split not implemented")
def test_active_and_inactive_sections_exist(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator("#sessions-active")).to_be_visible()
    expect(logged_in_page.locator("#sessions-inactive")).to_be_visible()


@pytest.mark.xfail(reason="spec: Inactive section collapsed by default")
def test_inactive_section_starts_collapsed(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    inactive = logged_in_page.locator("#sessions-inactive")
    assert "expanded" not in (inactive.get_attribute("class") or "")


@pytest.mark.xfail(reason="spec: clicking Inactive h2 toggles expanded")
def test_clicking_inactive_header_toggles(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    header = logged_in_page.locator("#sessions-inactive > h2")
    header.click()
    expect(logged_in_page.locator("#sessions-inactive.expanded")).to_be_visible()
    header.click()
    expect(logged_in_page.locator("#sessions-inactive.expanded")).to_have_count(0)


@pytest.mark.xfail(reason="spec: deactivate-btn on active cards (no confirm)")
def test_active_card_x_moves_to_inactive(logged_in_page, spawned_session):
    sid = spawned_session(name="moves-to-inactive")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
           ).to_be_visible(timeout=5000)
    logged_in_page.locator(
        f"#sessions-active [data-id='{sid}'] .deactivate-btn"
    ).click()
    expect(logged_in_page.locator(f"#sessions-active [data-id='{sid}']")
           ).to_have_count(0, timeout=5000)
    expect(logged_in_page.locator(f"#sessions-inactive [data-id='{sid}']")
           ).to_have_count(1)


# ===== New session modal (§2.2) =====

@pytest.mark.xfail(reason="spec: New session inline form moved to modal")
def test_new_button_opens_modal(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    logged_in_page.locator("#new-btn").click()
    expect(logged_in_page.locator("#modal-new-session")).to_be_visible()


@pytest.mark.xfail(reason="spec: modal closes via × / cancel / esc")
def test_new_session_modal_closes_on_esc(logged_in_page):
    logged_in_page.locator("#new-btn").click()
    expect(logged_in_page.locator("#modal-new-session")).to_be_visible()
    logged_in_page.keyboard.press("Escape")
    expect(logged_in_page.locator("#modal-new-session")).to_be_hidden()


# ===== Four permission modes (§12) =====

@pytest.mark.xfail(reason="spec: 4 modes (manual/accept_edits/plan/allow_all)")
def test_perm_menu_has_four_modes(logged_in_page, spawned_session):
    sid = spawned_session(name="perm-mode-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.card_by_id(sid).click()

    logged_in_page.locator("#chat-perm").click()
    items = logged_in_page.locator("#perm-menu .perm-menu-item")
    expect(items).to_have_count(4)
    modes = items.evaluate_all("els => els.map(e => e.dataset.mode)")
    assert set(modes) == {"manual", "accept_edits", "plan", "allow_all"}


# ===== Session card redesign (§2 cards) =====

@pytest.mark.xfail(reason="spec: card has .state-dot leading indicator")
def test_card_has_state_dot(logged_in_page, spawned_session):
    sid = spawned_session(name="state-dot-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    expect(card.locator(".state-dot")).to_have_count(1)


@pytest.mark.xfail(reason="spec: badge always shows, not just waiting/needs_input")
def test_card_badge_always_visible(logged_in_page, spawned_session):
    sid = spawned_session(name="badge-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(hp.card_by_id(sid).locator(".badge, .state-badge")
           ).to_be_visible(timeout=5000)


@pytest.mark.xfail(reason="spec: short id (ccr-XXXXXX) replaces full id")
def test_card_uses_short_id(logged_in_page, spawned_session):
    sid = spawned_session(name="short-id-test")
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    # short-id text length is 'ccr-' + 6 chars = 10 chars
    short = card.locator(".short-id")
    expect(short).to_have_count(1)
    assert len(short.inner_text().strip()) == 10


@pytest.mark.xfail(reason="spec: cwd shortened to last 2 segments")
def test_card_uses_cwd_short(logged_in_page, spawned_session, tmp_path):
    # tmp_path = /tmp/pytest-of-USER/pytest-N/test_X — last 2 segments = "pytest-N/test_X"
    sid = spawned_session(name="cwd-short-test", cwd=str(tmp_path))
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    short = card.locator(".cwd-short").inner_text()
    # Should be just the last two path components, not the full path
    assert "/" not in short.lstrip("/").split("/", 2)[2:] if "/" in short else True
    assert len(short) < len(str(tmp_path))


# ===== Recent cwds chips (§2 chip behavior) =====

@pytest.mark.xfail(reason="spec: chips are recent (LRU), not hardcoded presets")
def test_first_visit_no_chips(fresh_page, test_token, base_url):
    fresh_page.evaluate(f'() => localStorage.setItem("ccr.token", {test_token!r})')
    fresh_page.goto(base_url)
    expect(fresh_page.locator("#view-home")).to_be_visible()
    # No history → no chips
    expect(fresh_page.locator("#cwd-presets .chip")).to_have_count(0)


@pytest.mark.xfail(reason="spec: spawn appends to recent list (max 10, left=newest)")
def test_spawn_adds_to_recent_chips(logged_in_page, spawned_session, tmp_path):
    sid = spawned_session(name="adds-chip", cwd=str(tmp_path))
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    # After spawn, that cwd should be in the recent list
    recents = logged_in_page.evaluate(
        '() => JSON.parse(localStorage.getItem("ccr.recentCwds") || "[]")'
    )
    assert str(tmp_path) in recents
    assert recents[0] == str(tmp_path), "newest should be leftmost"


# ===== Search button (§2.3) =====

def test_search_button_opens_bar(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator("#search-btn")).to_be_visible()
    expect(logged_in_page.locator("#search-bar")).to_be_hidden()
    logged_in_page.locator("#search-btn").click()
    expect(logged_in_page.locator("#search-bar")).to_be_visible()
    expect(logged_in_page.locator("#search-input")).to_be_focused()


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

@pytest.mark.xfail(reason="spec: sort toggle between created / active")
def test_sort_toggle_exists(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator("#sessions-sort")).to_be_visible()


# ===== Ctx 不警示 (§11) =====

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
