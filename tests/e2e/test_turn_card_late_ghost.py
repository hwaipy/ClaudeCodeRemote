"""迟到幽灵卡: backlog_done 之后 (live mode), /resume 重放上一轮消息被
server 当新轮 → handleUserInput → _ensureTurnCard 实时建一张 active 卡,
紧挨在真实 summary 卡 (finalized) 下面. settle 点的相邻去重已经过去了,
所以要靠 live MutationObserver 实时收掉它.

表现: 上面一张不闪 (finalized summary), 下面一张闪 (active 幽灵)."""
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
    page.wait_for_timeout(150)


def test_late_active_ghost_collapsed_live(
    logged_in_page, base_url, test_token
):
    """真实 summary 卡已在 (finalized), 然后 live 模式下 _ensureTurnCard
    建一张相邻 active 幽灵 (不同 key) → MutationObserver 实时相邻去重收掉它."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "late-ghost")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            if (state._turnCardObserver) {
              state._turnCardObserver.disconnect();
              state._turnCardObserver = null;
            }
            // 确保 live-dedup observer 装着 (enterChat 已装, 这里兜底)
            _installTurnCardDedupObserver();
            // live 模式
            state.isHistoryReplay = false;
            state.earlierFragment = null;

            // 1) 真实最后一轮的 finalized summary 卡 (backlog 已渲)
            const realTs = Date.now() / 1000 - 320;
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: realTs, turn_ended_at: Date.now()/1000 - 11,
              output_tokens: 5990, model: 'claude-opus-4-7',
            }, Date.now()/1000 - 11);

            // 2) /resume 重放 → server 当新轮 → live user_input + turn_state
            //    建一张 active 幽灵卡 (不同 key, 紧挨真实卡)
            const ghostTs = Date.now() / 1000;   // resume 时刻的新时间戳
            state.turnStartAt = Math.round(ghostTs * 1000);
            state.turnEndAt = null;
            applyTurnState({ turn_started_at: ghostTs, turn_ended_at: null });

            // 等 MutationObserver 的 microtask 跑
            await new Promise(r => requestAnimationFrame(
              () => requestAnimationFrame(r)));
            await new Promise(r => setTimeout(r, 30));

            const cards = Array.from(
              log.querySelectorAll(':scope > .turn-card')
            ).map(c => ({
              key: c.dataset.turnStart,
              by: c.dataset.createdBy || '',
              active: c.classList.contains('turn-active'),
            }));
            return { total: cards.length, cards };
          }
        """)
        assert result["total"] == 1, (
            f"迟到的 active 幽灵卡应被实时收掉, 只剩 1 张真实 summary, "
            f"got {result['total']}: {result['cards']!r}"
        )
        assert result["cards"][0]["by"] == "_renderTurnSummary", (
            f"保留的应是真实 summary 卡, got {result['cards'][0]!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
