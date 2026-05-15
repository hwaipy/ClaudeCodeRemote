"""
Spec §2: "active N ago" text on each session card must tick in real time
(1 s minimum cadence) even when no new WS message arrives — so a card
that's been idle for 30 s shows "30s", not "1s" frozen at page load.

We don't sleep for 60 s; instead we inject a session_state that backdates
last_activity_at, then watch the .ts span tick through several values.
"""
from __future__ import annotations

import re
import time

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


def _ts_text(page, sid: str) -> str:
    return page.evaluate(
        f"() => document.querySelector(`[data-id='{sid}'] .ts`)?.textContent || ''"
    )


def _inject_la(page, sid, last_activity_at) -> None:
    page.evaluate(
        """({sid, la}) => {
            handleGlobalMsg({
              type: 'session_state',
              id: sid,
              name: 'ticker',
              cwd: '/tmp',
              created_at: la - 100,
              last_activity_at: la,
              state: 'idle',
              is_inactive: false,
              pending_permissions: 0,
              needs_action_detail: null,
            });
        }""",
        {"sid": sid, "la": last_activity_at},
    )


def test_active_ago_label_ticks_every_second(logged_in_page, base_url, test_token):
    """After backdating a card's last_activity_at by 30 s, the displayed
    "Ns ago" text must increment within ~2 s without any new WS event —
    proves the ticker is wired and not just a render-on-event refresh."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "ticker")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        expect(page.locator(f"[data-id='{sid}']")).to_be_visible(timeout=5000)

        # Backdate to 30 s ago — display should read "30s ago" right after
        # the inject's re-render.
        anchor = time.time() - 30
        _inject_la(page, sid, anchor)
        page.wait_for_timeout(50)
        before = _ts_text(page, sid)
        m = re.match(r"(\d+)s ago$", before)
        assert m is not None, f"expected 'Ns ago', got {before!r}"
        n_before = int(m.group(1))
        # Should be ~30 (allow ±2 for inject latency)
        assert 28 <= n_before <= 33, f"initial label off: {before!r}"

        # The ticker fires on fixed 1-s boundaries from page load, so a
        # 2-s wait only crosses one boundary. Wait 3.2 s to guarantee at
        # least 2 ticks fire.
        page.wait_for_timeout(3200)
        after = _ts_text(page, sid)
        m2 = re.match(r"(\d+)s ago$", after)
        assert m2 is not None, f"expected 'Ns ago' after wait, got {after!r}"
        n_after = int(m2.group(1))
        assert n_after >= n_before + 2, (
            f"label did not tick: was {before!r}, now {after!r} "
            f"(expected at least {n_before + 2}s)"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_active_ago_label_crosses_seconds_to_minutes_boundary(
    logged_in_page, base_url, test_token
):
    """When activity crosses the 60 s boundary the label should change
    units from 'Ns' to 'Nm' (per relTime in app.js) — the ticker must
    re-run relTime() each tick, not just stringify a captured number."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "boundary")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        expect(page.locator(f"[data-id='{sid}']")).to_be_visible(timeout=5000)

        # Backdate to 58 s ago → tick should cross into "1m" within ~3 s.
        _inject_la(page, sid, time.time() - 58)
        page.wait_for_timeout(50)
        before = _ts_text(page, sid)
        assert "s ago" in before, f"expected seconds label, got {before!r}"

        # Wait 3.5 s so we cross at least one tick past the 60-s boundary.
        page.wait_for_timeout(3500)
        after = _ts_text(page, sid)
        assert "m ago" in after, (
            f"label should have switched to minutes; was {before!r}, "
            f"now {after!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
