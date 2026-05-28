"""真实诊断数据复现: 两张 turn-card 直接相邻, 内容相同 (tokens+time) 但
key 不同. 来源:
  - 卡 A: _renderTurnSummary (backlog 重放真实轮次 turn_summary), key=真实 turn_started_at
  - 卡 B: _ensureTurnCard (实时, /resume 重放消息被 server 当新轮, 幽灵 key)
按 key 的去重永远碰不到它们 (key 不同). 但正常情况下 turn-card 之间一定隔
着消息气泡, 两张紧挨 = dup. 用相邻判据收掉幽灵, 保留真实 summary 卡."""
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


def test_adjacent_diff_key_same_content_collapsed(
    logged_in_page, base_url, test_token
):
    """精确复刻诊断 JSON 的末尾两张卡 → 跑去重 → 应只剩真实 summary 卡."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "adj-dup")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            // 前面放一个普通气泡 + 一张正常 turn-card (不该被动)
            const bubble = document.createElement('div');
            bubble.className = 'bubble assistant';
            bubble.textContent = 'earlier turn';
            log.appendChild(bubble);
            const normal = document.createElement('div');
            normal.className = 'turn-card';
            normal.dataset.turnStart = '1779974484061';
            normal.innerHTML = '<span class="turn-card-icon"></span>'
              + '<span class="turn-card-tokens">↓30,163t</span>'
              + '<span class="turn-card-time">32m53s</span>';
            log.appendChild(normal);
            // 一个分隔气泡 (真实轮次之间有内容)
            const b2 = document.createElement('div');
            b2.className = 'bubble user';
            b2.textContent = 'last turn input';
            log.appendChild(b2);
            // 卡 A: 真实 summary (replay)
            const cardA = document.createElement('div');
            cardA.className = 'turn-card';
            cardA.dataset.turnStart = '1779979413048';
            cardA.dataset.createdBy = '_renderTurnSummary';
            cardA.innerHTML = '<span class="turn-card-icon"></span>'
              + '<span class="turn-card-tokens">↓5,990t</span>'
              + '<span class="turn-card-time">5m9s</span>';
            log.appendChild(cardA);
            // 卡 B: 幽灵 (live _ensureTurnCard), key 不同, 内容相同, 紧挨 A
            const cardB = document.createElement('div');
            cardB.className = 'turn-card';
            cardB.dataset.turnStart = '1779979977146';
            cardB.dataset.createdBy = '_ensureTurnCard';
            cardB.innerHTML = '<span class="turn-card-icon"></span>'
              + '<span class="turn-card-tokens">↓5,990t</span>'
              + '<span class="turn-card-time">5m9s</span>';
            log.appendChild(cardB);

            // 跑去重
            if (typeof _dedupeAdjacentTurnCards === 'function') {
              _dedupeAdjacentTurnCards();
            }
            const cards = Array.from(
              log.querySelectorAll(':scope > .turn-card')
            ).map(c => ({
              key: c.dataset.turnStart,
              by: c.dataset.createdBy || '',
            }));
            return {
              total: cards.length,
              cards,
              normalSurvives: cards.some(c => c.key === '1779974484061'),
              realSurvives: cards.some(c => c.key === '1779979413048'),
              ghostGone: !cards.some(c => c.key === '1779979977146'),
            };
          }
        """)
        assert result["total"] == 2, (
            f"应剩 2 张 (正常的 32m53s + 真实最后一轮), got {result['total']}: "
            f"{result['cards']!r}"
        )
        assert result["normalSurvives"], "不相邻的正常卡不该被动"
        assert result["realSurvives"], (
            "真实 summary 卡 (_renderTurnSummary) 应保留"
        )
        assert result["ghostGone"], (
            "幽灵卡 (_ensureTurnCard, 不同 key 同内容相邻) 应被收掉"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
