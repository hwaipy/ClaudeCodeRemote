"""§2: a busy session with stale last_activity_at (no visible output
for STALLED_BUSY_THRESHOLD_S = 5 s) flips its state-dot from green to
yellow via a .stalled class added by the 1 s ticker."""
from __future__ import annotations

import time

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


def test_busy_with_stale_la_gains_stalled_class(
    logged_in_page, base_url, test_token
):
    """Directly mutate state.sessionsById entry to simulate a busy
    session with LA 10s in the past, then wait one ticker tick and
    verify the card has both .state-busy AND .stalled."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "stalled-test")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        card = page.locator(f"[data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)

        # Force the session into a "busy but stale" state in the client
        # cache. Use 10s in the past so we're well beyond the 5s threshold.
        page.evaluate(
            """(sid) => {
              const s = state.sessionsById.get(sid);
              s.state = "busy";
              s.last_activity_at = Date.now() / 1000 - 400;
              // Re-render so the card gets .state-busy applied
              renderSessionList && renderSessionList();
            }""",
            sid,
        )
        # Wait for the next 1s ticker tick to evaluate stalled-ness
        page.wait_for_timeout(1200)
        # The card MUST be both busy AND stalled
        cls = (card.get_attribute("class") or "")
        assert "state-busy" in cls, f"card should be busy: {cls}"
        assert "stalled" in cls, f"card should be stalled: {cls}"
        # The dot's computed background should be the yellow (#d29922 ~ rgb(210,153,34))
        bg = page.evaluate(
            f"() => getComputedStyle(document.querySelector("
            f"'[data-id=\"{sid}\"] .state-dot')).backgroundColor"
        )
        # Allow some browser color normalization
        assert "210" in bg and "153" in bg and "34" in bg, (
            f"dot should be yellow (#d29922 ≈ rgb(210,153,34)), got {bg}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_busy_with_fresh_la_does_not_stall(
    logged_in_page, base_url, test_token
):
    """Inverse: a busy session whose LA is recent (under 5s) must NOT
    have .stalled and the dot stays green."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "not-stalled-test")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        card = page.locator(f"[data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        page.evaluate(
            """(sid) => {
              const s = state.sessionsById.get(sid);
              s.state = "busy";
              s.last_activity_at = Date.now() / 1000;   // right now
              renderSessionList && renderSessionList();
            }""",
            sid,
        )
        page.wait_for_timeout(1200)
        cls = (card.get_attribute("class") or "")
        assert "state-busy" in cls, f"card should be busy: {cls}"
        assert "stalled" not in cls, f"card must NOT be stalled: {cls}"
        bg = page.evaluate(
            f"() => getComputedStyle(document.querySelector("
            f"'[data-id=\"{sid}\"] .state-dot')).backgroundColor"
        )
        # green #3fb950 ≈ rgb(63,185,80)
        assert "63" in bg and "185" in bg and "80" in bg, (
            f"dot should be green (#3fb950 ≈ rgb(63,185,80)), got {bg}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
