"""E2E version of the A→B switch bug:
Spawn two real sessions via fake_claude, run a turn in each, then in the UI
click A first → click B → assert B's chat-log has exactly 1 turn-card.
This catches state pollution / event ordering issues that the unit-style
event-injection test might miss.
"""
from __future__ import annotations

import re
import time

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
    page.wait_for_timeout(150)


def _back_home(page):
    # On wide-screen layout (>= 900px), chat-back is hidden but click still works.
    page.locator("#chat-back").dispatch_event("click")
    expect(page.locator("body")).not_to_have_class(
        re.compile(r"\bhas-session\b"), timeout=5000
    )
    page.wait_for_timeout(150)


def _send_one_turn(page, sid):
    """Send a message via chat-input + wait for a turn-card to settle."""
    ta = page.locator("#chat-input")
    ta.focus()
    page.keyboard.type("hello")
    page.keyboard.press("Enter")
    # Wait until a turn-card appears AND becomes finalized.
    page.wait_for_function(
        """() => {
          const log = document.getElementById('chat-log');
          if (!log) return false;
          const card = log.querySelector('.turn-card');
          return card && !card.classList.contains('turn-active');
        }""",
        timeout=15000,
    )


def test_a_then_b_no_dup_turn_cards(
    logged_in_page, base_url, test_token
):
    """Real UI flow: refresh → enter A (let it finish a turn) → click B
    (which has MULTIPLE turns of history) → assert no dup turn-cards.
    Multi-turn B is key — bug only manifests when applyTurnState/_refreshTurnCard
    create dup cards for older turns during replay."""
    page = logged_in_page
    sid_a = api_spawn(base_url, test_token, "/tmp", "session-A")
    sid_b = api_spawn(base_url, test_token, "/tmp", "session-B")
    try:
        # 1. Enter A and run one turn so A has history
        _enter_chat(page, sid_a)
        _send_one_turn(page, sid_a)
        page.wait_for_timeout(200)

        # 2. Back to home (chat-back)
        _back_home(page)

        # 3. Enter B and run THREE turns (multi-turn history triggers the bug)
        _enter_chat(page, sid_b)
        for _ in range(3):
            _send_one_turn(page, sid_b)
            page.wait_for_timeout(200)

        # 4. Hard-refresh to clear in-memory cache (mimic user's "刷新" step)
        page.reload()
        expect(page.locator("#view-home")).to_be_visible(timeout=10000)
        page.wait_for_timeout(300)

        # 5. Enter A first
        _enter_chat(page, sid_a)
        page.wait_for_timeout(500)   # let backlog settle

        # 6. Click session B (real switch — keeps state.sessionId)
        _back_home(page)
        _enter_chat(page, sid_b)
        # Let backlog_done + earlierFragment prepend settle
        page.wait_for_timeout(1200)

        # 7. Assert: B should have exactly 1 turn-card (its one finished turn)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            const cards = Array.from(
              log.querySelectorAll(':scope > .turn-card')
            );
            const seen = new Map();
            const dups = [];
            for (const c of cards) {
              const k = c.dataset.turnStart || '(no-key)';
              if (seen.has(k)) dups.push({ key: k });
              else seen.set(k, true);
            }
            return {
              total: cards.length,
              uniqueKeys: seen.size,
              dups,
              activeCount: cards.filter(
                c => c.classList.contains('turn-active')
              ).length,
            };
          }
        """)
        # B has 3 turns; should have exactly 3 turn-cards (no dup).
        assert result["total"] == 3, (
            f"Expected 3 turn-cards (one per B's turn), "
            f"got {result['total']} (dups={result['dups']!r})"
        )
        assert result["uniqueKeys"] == 3, (
            f"DUPLICATE turn-cards detected: total={result['total']}, "
            f"unique={result['uniqueKeys']}, dups={result['dups']!r}"
        )
        # B is idle (turn ended); no card should be flashing
        assert result["activeCount"] == 0, (
            f"No turn-card should be .turn-active for an idle session, "
            f"got {result['activeCount']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)
