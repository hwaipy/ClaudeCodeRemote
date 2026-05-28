"""根因: _ensureTurnCard 的 fallback (line ~3300) 在指定了 key 但没找到
同 key 卡时, 会去接管 chat-log 里**任意**一张 .turn-active 卡, 然后把它的
dataset.turnStart 覆盖成新 key — 偷了另一个 turn 的卡, 改了它的身份.

后果链 (active A → ended B 末尾两张相同 token-card):
B 重放时若有一张残留 active 卡 (turn Y), 后续 _ensureTurnCard(key=X) 偷走
它标成 X. 真正属于 Y 的 turn_summary 再来时找不到 Y 卡 → 新建一张 Y;
属于 X 的 summary 找到被偷的卡. 卡身份错乱 → 末尾出现同值重复卡."""
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


def test_ensure_does_not_steal_other_turns_active_card(
    logged_in_page, base_url, test_token
):
    """有一张 turn Y 的 active 卡; _ensureTurnCard(key=X, X≠Y) 必须新建 X 卡,
    不能把 Y 卡的 dataset.turnStart 改成 X."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "key-steal")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            state.earlierFragment = null;
            state.isHistoryReplay = false;
            const KEY_Y = "1000000000000";
            const KEY_X = "2000000000000";
            // 1) 手动放一张 turn Y 的 active 卡 (模拟 B 重放出一张残留 active)
            const cardY = document.createElement('div');
            cardY.className = 'turn-card turn-active';
            cardY.dataset.turnStart = KEY_Y;
            cardY.innerHTML = '<span class="turn-card-icon"></span>'
              + '<span class="turn-card-tokens">↓5t</span>'
              + '<span class="turn-card-time">3s</span>';
            log.appendChild(cardY);
            // state._turnCard 指向别处 (模拟 ref 丢失/不一致)
            state._turnCard = null;
            // 2) 现在为另一个 turn X ensure 卡
            state.turnStartAt = Number(KEY_X);
            state.turnEndAt = null;
            _ensureTurnCard();
            // 检查: cardY 的 key 还是 Y 吗? 有没有新建一张 X 卡?
            const cards = Array.from(log.querySelectorAll(':scope > .turn-card'))
              .map(c => ({ key: c.dataset.turnStart,
                           active: c.classList.contains('turn-active') }));
            return {
              cardYStillY: cardY.dataset.turnStart === KEY_Y,
              cards,
              total: cards.length,
              hasXCard: cards.some(c => c.key === KEY_X),
            };
          }
        """)
        assert result["cardYStillY"], (
            f"turn Y 的卡被偷改了 key! cards={result['cards']!r}"
        )
        assert result["hasXCard"], (
            f"应为 turn X 新建一张卡, cards={result['cards']!r}"
        )
        assert result["total"] == 2, (
            f"应有 2 张卡 (Y + X 各一), got {result['total']}: {result['cards']!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
