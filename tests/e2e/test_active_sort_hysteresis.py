"""
Spec §2: when sort mode = "active", a session whose last_activity_at jumps
by ≤ 180s should NOT change its position in the list. Only a jump > 180s
re-orders.

We inject synthetic session_state messages via window.handleGlobalMsg
because the server only updates last_activity_at when real envelopes flow
through _pump, and we want deterministic timestamps.
"""
from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


def _card_order(page) -> list[str]:
    """Return [session_id, …] in the order they appear in #session-list-active."""
    return page.evaluate(
        """() => Array.from(
            document.querySelectorAll('#session-list-active .session-card')
        ).map(c => c.dataset.id)"""
    )


def _set_sort_active(page) -> None:
    """Force sort mode to 'active' (clock icon)."""
    page.evaluate(
        """() => {
            try { localStorage.setItem('ccr.sortMode', 'active'); } catch (e) {}
            if (typeof setSortMode === 'function') setSortMode('active');
            else if (typeof renderSessionList === 'function') renderSessionList();
        }"""
    )


def _inject_session_state(page, sid, last_activity_at, *, name="ses", state_value="idle") -> None:
    """Synthesize a session_state WS message and feed it to handleGlobalMsg."""
    page.evaluate(
        """({sid, la, name, st}) => {
            handleGlobalMsg({
              type: 'session_state',
              id: sid,
              name: name,
              cwd: '/tmp',
              created_at: la - 100,
              last_activity_at: la,
              state: st,
              is_inactive: false,
              pending_permissions: 0,
              needs_action_detail: null,
            });
        }""",
        {"sid": sid, "la": last_activity_at, "name": name, "st": state_value},
    )


def test_active_sort_small_bump_does_not_reorder(logged_in_page, base_url, test_token):
    """Bumping a session's last_activity_at by ≤ 180s in 'active' sort mode
    must NOT change its list position."""
    page = logged_in_page
    now = time.time()
    # Spawn 2 sessions. Their server-side last_activity_at will both be
    # ~now (within milliseconds), so we override via injected WS messages.
    sid_a = api_spawn(base_url, test_token, "/tmp", "AAA")
    sid_b = api_spawn(base_url, test_token, "/tmp", "BBB")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        expect(page.locator(f"[data-id='{sid_a}']")).to_be_visible(timeout=5000)
        expect(page.locator(f"[data-id='{sid_b}']")).to_be_visible(timeout=5000)
        _set_sort_active(page)
        # Inject distinct timestamps: A is OLDER, B is NEWER → expect [B, A].
        _inject_session_state(page, sid_a, now - 600, name="AAA")  # 10 min ago
        _inject_session_state(page, sid_b, now - 60, name="BBB")    # 1 min ago
        order = _card_order(page)
        a_pos = order.index(sid_a)
        b_pos = order.index(sid_b)
        assert b_pos < a_pos, (
            f"initial active-sort order should put newer B above A; got {order}"
        )

        # Bump A by 100 s (< 180 s hysteresis). Order MUST stay [..., B, ..., A, ...].
        _inject_session_state(page, sid_a, now - 600 + 100, name="AAA")
        order_after = _card_order(page)
        assert order_after.index(sid_b) < order_after.index(sid_a), (
            f"after a sub-180s bump on A, A should still be below B; "
            f"got {order_after}"
        )
        # Specifically A's position must be unchanged.
        assert order_after.index(sid_a) == a_pos, (
            f"A's position changed despite < 180s bump: was {a_pos}, now "
            f"{order_after.index(sid_a)}"
        )
    finally:
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)


def test_active_sort_large_bump_reorders(logged_in_page, base_url, test_token):
    """Bumping a session's last_activity_at by > 180s in 'active' sort mode
    MUST update the sort snapshot and move the card to the top."""
    page = logged_in_page
    now = time.time()
    sid_a = api_spawn(base_url, test_token, "/tmp", "AAA")
    sid_b = api_spawn(base_url, test_token, "/tmp", "BBB")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        expect(page.locator(f"[data-id='{sid_a}']")).to_be_visible(timeout=5000)
        expect(page.locator(f"[data-id='{sid_b}']")).to_be_visible(timeout=5000)
        _set_sort_active(page)
        # Initial injects pick timestamps that are BOTH > 180s from the
        # snapshot LA (which is roughly `now`, the spawn moment), so both
        # snapshots actually update to the injected values. Otherwise B's
        # snapshot would stay at spawn-time and the test would be racy.
        _inject_session_state(page, sid_a, now - 1000, name="AAA")
        _inject_session_state(page, sid_b, now - 500, name="BBB")
        order = _card_order(page)
        assert order.index(sid_b) < order.index(sid_a), (
            f"setup: B should be above A; got {order}"
        )

        # Bump A by 1000s (> 180s). Snapshot updates → A above B.
        _inject_session_state(page, sid_a, now, name="AAA")
        order_after = _card_order(page)
        assert order_after.index(sid_a) < order_after.index(sid_b), (
            f"after a > 180s bump on A, A should be above B; got {order_after}"
        )
    finally:
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)


def test_active_sort_hysteresis_holds_across_many_small_bumps(
    logged_in_page, base_url, test_token
):
    """A session getting many small (<180s) bumps in succession must stay
    pinned to its snapshot — the snapshot doesn't drift forward step by
    step. Only an absolute jump from the snapshot > 180s rolls it forward."""
    page = logged_in_page
    now = time.time()
    sid_a = api_spawn(base_url, test_token, "/tmp", "AAA")
    sid_b = api_spawn(base_url, test_token, "/tmp", "BBB")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        expect(page.locator(f"[data-id='{sid_a}']")).to_be_visible(timeout=5000)
        expect(page.locator(f"[data-id='{sid_b}']")).to_be_visible(timeout=5000)
        _set_sort_active(page)
        # Both initial offsets are > 180s from the spawn moment so each
        # session's snapshot lands on the injected value, deterministically.
        _inject_session_state(page, sid_a, now - 600, name="AAA")
        _inject_session_state(page, sid_b, now - 400, name="BBB")
        a_pos = _card_order(page).index(sid_a)

        # Five small bumps on A, each +150s above the previous LA. Each
        # single delta is 150 < 180, so the contract says the snapshot
        # NEVER moves — even though cumulatively the LA travels +750s.
        for i in range(1, 6):
            _inject_session_state(page, sid_a, now - 600 + i * 150, name="AAA")
        order_after = _card_order(page)
        assert order_after.index(sid_a) == a_pos, (
            f"after 5 cumulative sub-180s bumps, A must still be at the same "
            f"position (snapshot is sticky, not drifting). was {a_pos}, "
            f"now {order_after.index(sid_a)}; full order: {order_after}"
        )
        assert order_after.index(sid_b) < order_after.index(sid_a), (
            f"B should still be above A; got {order_after}"
        )
    finally:
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)
