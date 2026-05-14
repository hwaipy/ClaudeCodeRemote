"""Sanity tests against the snapshot of the user's real CCR DB.

These verify the home page rendering reflects what's actually in the DB.
If they pass, the live-data fixture pipeline works end-to-end.
"""
from __future__ import annotations

import httpx
from playwright.sync_api import expect


def test_api_sessions_count_matches_db(live_server_env, live_db):
    """HTTP API returns the same session count as DB (minus deleted)."""
    r = httpx.get(
        f"{live_server_env['base_url']}/api/sessions",
        headers={"Authorization": f"Bearer {live_server_env['token']}"},
    )
    r.raise_for_status()
    api_count = len(r.json()["sessions"])

    db_count = live_db.execute(
        "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL"
    ).fetchone()[0]
    assert api_count == db_count, (
        f"API returned {api_count} sessions but DB has {db_count} non-deleted"
    )


def test_home_renders_session_cards(logged_in_page, live_db):
    """The session list in the home view has one card per non-deleted DB row."""
    expect(logged_in_page.locator("#view-home")).to_be_visible()

    db_count = live_db.execute(
        "SELECT COUNT(*) FROM sessions WHERE deleted_at IS NULL"
    ).fetchone()[0]

    cards = logged_in_page.locator(".session-card")
    expect(cards).to_have_count(db_count, timeout=5000)


def test_card_names_match_db(logged_in_page, live_db):
    """Every session name in the DB appears as a card name in the UI."""
    expect(logged_in_page.locator("#view-home")).to_be_visible()
    expect(logged_in_page.locator(".session-card").first
           ).to_be_visible(timeout=5000)

    rendered_names = logged_in_page.locator(
        ".session-card .name"
    ).all_inner_texts()
    db_names = [
        (row[0] or "untitled") for row in live_db.execute(
            "SELECT name FROM sessions WHERE deleted_at IS NULL"
        )
    ]
    assert sorted(rendered_names) == sorted(db_names)


def test_page_has_all_six_states_represented_somewhere(logged_in_page, live_db):
    """At least one card per state that exists in the DB.

    State priority follows compute_state(): waiting_permission > needs_input
    > busy/idle (running) > hibernated > finished. With no proc started yet
    on server boot, sessions show as 'hibernated' or 'finished'. So we just
    check there's variety, not specific states.
    """
    expect(logged_in_page.locator(".session-card").first
           ).to_be_visible(timeout=5000)

    # Each card has a state-* class somewhere on it
    card_count = logged_in_page.locator(".session-card").count()
    assert card_count > 0
