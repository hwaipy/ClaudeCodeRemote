"""Repro for user-reported bug:
"刷新后, 先点开活动中的 session (turn-card 闪烁), 再点开不活动的 session
→ 末尾有连续两条一样的 finalized turn-card."

We simulate the WS backlog flow directly via page.evaluate, injecting events
that mirror what the per-session WS sends. The crucial bits:
- session B (ended) has multiple turns in its backlog.
- between switching A→B, state._turnCard is reset by enterChat at line 2315.
- backlog plays: first_paint snapshot (latest turn), then earlier batch
  (older turns), then backlog_done.

After backlog_done settles, chat-log should have exactly ONE turn-card per
distinct turn_started_at — never two with the same key.
"""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
    page.wait_for_timeout(200)


def test_no_dup_turn_cards_when_replaying_multi_turn_ended_session(
    logged_in_page, base_url, test_token
):
    """直接模拟 B 的 backlog 事件流: 3 个完整 turn + first_paint(latest) +
    backlog_done. 期望: 共 3 张 turn-card, 没有 dup."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "no-dup-cross")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            // 重置 state 模拟 enterChat 冷启动 (state.turnStartAt=null 等)
            state.turnStartAt = null;
            state.turnEndAt = null;
            state._turnCard = null;
            if (state._turnCardObserver) {
              state._turnCardObserver.disconnect();
              state._turnCardObserver = null;
            }
            state.isHistoryReplay = true;
            state.earlierFragment = null;
            state.msgById = new Map();
            state.toolById = new Map();
            state.askuserById = new Map();
            state.blocksByIdx = new Map();
            // 模拟 sessionsById 里有 B 的 metadata, state="idle"
            state.sessionsById.set(state.sessionId, {
              id: state.sessionId, state: "idle",
            });

            const base = Date.now() / 1000 - 1000;
            // 3 个 turn 的时间戳
            const T1s = base + 0, T1e = base + 10;
            const T2s = base + 100, T2e = base + 110;
            const T3s = base + 200, T3e = base + 210;

            // ====== Recent batch (T3 events) ======
            // 模拟 user_input + turn_state start + turn_state end + turn_summary
            handleEvent({ type: 'user_input', content: 'T3 user msg' }, T3s);
            handleEvent({
              type: '_ccr', subtype: 'turn_state',
              turn_started_at: T3s, turn_ended_at: null,
              model: 'claude-opus-4-7', output_tokens: 0,
            }, T3s);
            // 真 server 用 type: 'system' / 'assistant' / 'result' 之类,
            // 我们关心的是 turn-card lifecycle 事件:
            handleEvent({
              type: '_ccr', subtype: 'turn_state',
              turn_started_at: T3s, turn_ended_at: T3e,
              model: 'claude-opus-4-7', output_tokens: 333,
            }, T3e);
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: T3s, turn_ended_at: T3e,
              output_tokens: 333, model: 'claude-opus-4-7',
            }, T3e);

            // ====== first_paint marker (state snapshot of latest = T3) ======
            handleEvent({
              type: '_ccr', subtype: 'first_paint',
              turn_state: {
                turn_started_at: T3s, turn_ended_at: T3e,
                output_tokens: 333, model: 'claude-opus-4-7',
              },
            });

            // ====== Earlier batch (T1, T2 events) ======
            // Bubbles 会渲到 earlierFragment, turn-cards 走 _ensureTurnCard 到
            // chat-log (跨 root 的 bug), summaries 走 _renderTurnSummary 到
            // earlierFragment. 这是 dup 的根源.
            handleEvent({ type: 'user_input', content: 'T1 user msg' }, T1s);
            handleEvent({
              type: '_ccr', subtype: 'turn_state',
              turn_started_at: T1s, turn_ended_at: null,
              model: 'claude-sonnet-4-6', output_tokens: 0,
            }, T1s);
            handleEvent({
              type: '_ccr', subtype: 'turn_state',
              turn_started_at: T1s, turn_ended_at: T1e,
              model: 'claude-sonnet-4-6', output_tokens: 111,
            }, T1e);
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: T1s, turn_ended_at: T1e,
              output_tokens: 111, model: 'claude-sonnet-4-6',
            }, T1e);

            handleEvent({ type: 'user_input', content: 'T2 user msg' }, T2s);
            handleEvent({
              type: '_ccr', subtype: 'turn_state',
              turn_started_at: T2s, turn_ended_at: null,
              model: 'claude-sonnet-4-6', output_tokens: 0,
            }, T2s);
            handleEvent({
              type: '_ccr', subtype: 'turn_state',
              turn_started_at: T2s, turn_ended_at: T2e,
              model: 'claude-sonnet-4-6', output_tokens: 222,
            }, T2e);
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: T2s, turn_ended_at: T2e,
              output_tokens: 222, model: 'claude-sonnet-4-6',
            }, T2e);

            // ====== backlog_done ======
            handleEvent({
              type: '_ccr', subtype: 'backlog_done',
              first_seq: 1, has_more: false, history_count: 9,
            });
            await new Promise(r => setTimeout(r, 50));

            // 收集所有 turn-card 信息
            const cards = Array.from(log.querySelectorAll(':scope > .turn-card'));
            const out = cards.map(c => ({
              key: c.dataset.turnStart,
              active: c.classList.contains('turn-active'),
              tokens: c.querySelector('.turn-card-tokens')?.textContent || '',
            }));
            const keys = cards.map(c => c.dataset.turnStart);
            const keySet = new Set(keys);
            return {
              total: cards.length,
              uniqueKeys: keySet.size,
              cards: out,
            };
          }
        """)
        # 期望: 3 个 distinct turn, 每个 1 张 card, 共 3 张
        assert result["total"] == 3, (
            f"Expected 3 turn-cards total (one per distinct turn), "
            f"got {result['total']}: {result['cards']!r}"
        )
        assert result["uniqueKeys"] == 3, (
            f"Expected 3 unique keys, got {result['uniqueKeys']}: "
            f"{result['cards']!r}"
        )
        # 不应有任何 active 卡 (session idle, all turns ended)
        active = [c for c in result["cards"] if c["active"]]
        assert not active, f"No card should be active, got: {active!r}"
    finally:
        api_delete_session(base_url, test_token, sid)
