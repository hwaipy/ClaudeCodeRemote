"""L1 home view — empty state, logout, spawn (via API), delete card."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.pages.home_page import HomePage


def test_empty_session_list(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(hp.cards).to_have_count(0)


def test_logout_returns_to_login(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.logout.click()
    expect(logged_in_page.locator("#view-login")).to_be_visible()
    expect(logged_in_page.locator("#view-home")).to_be_hidden()
    assert hp.stored_token() in (None, "")


def test_spawned_session_appears_as_card(logged_in_page, spawned_session):
    """Spawn via API, watch WS push a session_state event, see card render."""
    sid = spawned_session(name="fixture-session")

    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(hp.card_by_id(sid)).to_be_visible(timeout=5000)
    expect(hp.card_by_id(sid).locator(".name")).to_have_text("fixture-session")


def test_delete_card_removes_it(logged_in_page, spawned_session):
    """Click the card's del-btn, accept confirm, verify the card disappears."""
    sid = spawned_session(name="to-delete")

    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)

    logged_in_page.once("dialog", lambda d: d.accept())
    card.locator(".del-btn").click()

    expect(card).to_have_count(0, timeout=5000)


def test_spawn_with_invalid_cwd_shows_error(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.fill_spawn_form(cwd="/nonexistent/path/" + "x" * 30, name="bad-cwd")
    hp.submit_spawn()
    expect(hp.spawn_err).to_be_visible(timeout=3000)
    # modal stays open on error
    expect(logged_in_page.locator("#modal-new-session")).to_be_visible()
    expect(hp.cards).to_have_count(0)


@pytest.mark.skip(reason="hard-reload involves SW + cache clear; race-y to assert")
def test_hard_reload(logged_in_page):
    # Placeholder — verifies link exists but doesn't actually click
    hp = HomePage(logged_in_page)
    expect(hp.hard_reload).to_be_visible()
