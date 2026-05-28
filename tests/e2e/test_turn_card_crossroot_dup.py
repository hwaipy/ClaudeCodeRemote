"""精确复现 'active A → ended B 末尾两张相同 turn-card' 的根因路径.

真实场景里 B 被重新打开时, 两条路径并发渲染同一 turn 的 summary:
  - IDB replay (chatRoot = chat-log, earlierFragment 还没设)
  - WS earlier batch (chatRoot = earlierFragment)
跨 root 的 dedup 如果漏了, 就各建一张同 key 卡, backlog_done prepend
后两张挨在一起.

这个测试直接驱动两条 root 的 _renderTurnSummary, 验证 dedup 是否跨
root 生效 (DocumentFragment 上的 :scope 选择器是重点怀疑对象)."""
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


def test_summary_dedup_across_chatlog_and_earlierfragment(
    logged_in_page, base_url, test_token
):
    """card_A 在 chat-log, card_B 试图在 earlierFragment 建同 key — dedup
    必须跨 root 命中, 最终 prepend 后只剩 1 张."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "crossroot-1")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            state.earlierFragment = null;
            const ts = Date.now() / 1000 - 5;
            const te = Date.now() / 1000;
            // 路径 1: IDB replay 视角 — chatRoot = chat-log
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: ts, turn_ended_at: te,
              output_tokens: 100, model: 'claude-opus-4-7',
            }, te);
            const afterFirst = log.querySelectorAll(':scope > .turn-card').length;
            // 路径 2: WS earlier batch 视角 — earlierFragment 设上,
            // chatRoot() 返回 earlierFragment
            state.earlierFragment = document.createDocumentFragment();
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: ts, turn_ended_at: te,
              output_tokens: 100, model: 'claude-opus-4-7',
            }, te);
            const inFragment = state.earlierFragment.querySelectorAll
              ? state.earlierFragment.querySelectorAll('.turn-card').length : -1;
            // 模拟 backlog_done: prepend earlierFragment 到 chat-log
            log.insertBefore(state.earlierFragment, log.firstChild);
            state.earlierFragment = null;
            // 跑 dedup (backlog_done 时会做)
            if (typeof _dedupeTurnCardsByKey === 'function') _dedupeTurnCardsByKey();
            const final = log.querySelectorAll(':scope > .turn-card').length;
            return { afterFirst, inFragment, final };
          }
        """)
        assert result["afterFirst"] == 1, (
            f"路径1 后 chat-log 应 1 张, got {result['afterFirst']}"
        )
        assert result["final"] == 1, (
            f"跨 root 同 key 应只剩 1 张 turn-card, got {result['final']} "
            f"(fragment 里建了 {result['inFragment']} 张 = dedup 跨 root 漏了)"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_ensure_then_summary_dedup_across_root(
    logged_in_page, base_url, test_token
):
    """另一组合: _ensureTurnCard 在 chat-log 建 active 卡, 然后
    earlierFragment 阶段 turn_summary 进来 — 也要跨 root 命中同一张."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "crossroot-2")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            state.earlierFragment = null;
            const ts = Date.now() / 1000 - 5;
            state.turnStartAt = ts * 1000;
            state.turnEndAt = null;
            // chat-log 建 active 卡
            _ensureTurnCard();
            const afterEnsure = log.querySelectorAll(':scope > .turn-card').length;
            // earlierFragment 阶段, summary 进来 (同 turn)
            state.earlierFragment = document.createDocumentFragment();
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: ts, turn_ended_at: Date.now()/1000,
              output_tokens: 50, model: 'claude-sonnet-4-6',
            }, Date.now()/1000);
            log.insertBefore(state.earlierFragment, log.firstChild);
            state.earlierFragment = null;
            if (typeof _dedupeTurnCardsByKey === 'function') _dedupeTurnCardsByKey();
            const final = log.querySelectorAll(':scope > .turn-card').length;
            return { afterEnsure, final };
          }
        """)
        assert result["afterEnsure"] == 1
        assert result["final"] == 1, (
            f"ensure(chat-log) + summary(fragment) 同 turn 应只剩 1 张, "
            f"got {result['final']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
