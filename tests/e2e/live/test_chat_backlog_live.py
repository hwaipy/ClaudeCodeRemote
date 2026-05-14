"""L2 chat view — open a real session, verify backlog renders.

Uses the live snapshot. Picks the session with the most messages (most likely
to exercise rich rendering: user/assistant/tool/result events).
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.pages.home_page import HomePage

ACTIVE_CLASS = re.compile(r"\bactive\b")
HAS_SESSION_CLASS = re.compile(r"\bhas-session\b")


@pytest.fixture(scope="module")
def biggest_session_id(live_db) -> str:
    """Session with the most messages in the snapshot."""
    row = live_db.execute("""
        SELECT s.id, s.name, COUNT(m.seq) AS n
        FROM sessions s
        LEFT JOIN messages m ON m.sess_id = s.id
        WHERE s.deleted_at IS NULL
        GROUP BY s.id
        ORDER BY n DESC
        LIMIT 1
    """).fetchone()
    assert row is not None, "no sessions in live fixture"
    return row["id"]


def test_clicking_card_opens_chat(logged_in_page, biggest_session_id):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    card = hp.card_by_id(biggest_session_id)
    expect(card).to_be_visible(timeout=5000)
    card.click()

    # body.stage-app keeps both views in DOM for swipe; .active is the truth
    expect(logged_in_page.locator("#view-chat")).to_have_class(
        ACTIVE_CLASS, timeout=5000
    )
    expect(logged_in_page.locator("#view-home")).not_to_have_class(
        ACTIVE_CLASS
    )
    expect(logged_in_page.locator("body")).to_have_class(HAS_SESSION_CLASS)


def test_chat_header_shows_session_name(logged_in_page, biggest_session_id, live_db):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expected_name = live_db.execute(
        "SELECT name FROM sessions WHERE id = ?", (biggest_session_id,)
    ).fetchone()["name"] or "untitled"
    hp.card_by_id(biggest_session_id).click()

    expect(logged_in_page.locator("#chat-name")).to_have_text(
        expected_name, timeout=5000
    )


def test_chat_renders_at_least_one_bubble(logged_in_page, biggest_session_id):
    """Backlog should paint user/assistant bubbles within a few seconds."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.card_by_id(biggest_session_id).click()

    bubbles = logged_in_page.locator("#chat-log .bubble")
    expect(bubbles.first).to_be_visible(timeout=10000)
    # any backlog at all
    assert bubbles.count() >= 1


def test_back_button_returns_to_home(logged_in_page, biggest_session_id):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.card_by_id(biggest_session_id).click()
    expect(logged_in_page.locator("body")).to_have_class(
        HAS_SESSION_CLASS, timeout=5000
    )

    # Playwright marks #chat-back hidden under chrome-headless even though
    # computed style is display:flex (header is position:fixed + viewport
    # heuristic). Dispatch the click event directly — the handler does the work.
    logged_in_page.locator("#chat-back").dispatch_event("click")

    expect(logged_in_page.locator("#view-home")).to_have_class(
        ACTIVE_CLASS, timeout=3000
    )
    expect(logged_in_page.locator("body")).not_to_have_class(
        HAS_SESSION_CLASS
    )
