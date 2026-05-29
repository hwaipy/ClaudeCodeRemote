"""重进 chat 末尾出现重复 user message 的复现.

可疑路径: cold path 下 IDB replay 与 WS backlog 都会渲 user_input bubble.
WS open 时 state.dedupeBoundary = state.maxSeq, 但 IDB replay 是 await,
如果 WS 先 open (短 RTT, 比 indexedDB 第一次冷启动还快), 边界 = 0 →
server backlog 全量重放, 之后 IDB replay 收尾再渲 → 同条 user_input 被
appendBubble 两次. appendBubble 没按 seq/idempotent key 去重.
"""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter(page, sid):
    card = page.locator(f"[data-id='{sid}']")
    expect(card).to_be_visible(timeout=10000)
    card.click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000)
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=10000)


def _send(page, text):
    page.locator("#chat-input").fill(text)
    page.evaluate("sendUserMessage()")


def test_reenter_after_reload_no_duplicate_user_bubble(
    logged_in_page, base_url, test_token,
):
    """3 条消息 → reload → 重进 → 应有恰好 3 条 user bubble (而不是 6)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "no-dup-natural")
    try:
        _enter(page, sid)
        page.wait_for_timeout(300)

        msgs = ["alpha-1", "bravo-2", "charlie-3"]
        for m in msgs:
            _send(page, m)
            page.wait_for_timeout(700)

        # 确认本会话先看到 3 条 user bubble
        page.wait_for_function(
            "() => document.querySelectorAll('.bubble.user').length === 3",
            timeout=5000,
        )

        # 硬刷, sessionCache 清掉. 进入 cold path → IDB replay + WS backlog 同时跑.
        page.reload()
        page.wait_for_selector(f"[data-id='{sid}']", timeout=10000)
        page.locator(f"[data-id='{sid}']").click()
        expect(page.locator("body")).to_have_class(
            re.compile(r"\bhas-session\b"), timeout=10000)
        # 等 backlog_done + IDB replay 都落定
        page.wait_for_selector(".bubble.user", timeout=5000)
        page.wait_for_timeout(2000)

        bubbles = page.evaluate(
            "[...document.querySelectorAll('.bubble.user')].map("
            "b => (b.textContent || '').trim())"
        )
        # 必须恰好 3 条 (而非 6 / 多份)
        assert len(bubbles) == 3, (
            f"重进后应有 3 条 user bubble, 实际 {len(bubbles)}: {bubbles}"
        )
        # 内容也对齐, 不是空 bubble 凑数
        for m in msgs:
            assert m in bubbles, f"missing {m} in {bubbles}"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_reenter_with_slow_idb_race(
    logged_in_page, base_url, test_token,
):
    """强制 race: 注入 idbGetSessionMessages 延迟, WS 必然先 open.
    此时 dedupeBoundary=0, server backlog 全部重放 → 然后 IDB 收尾重渲 →
    应仍只有 3 条 user bubble (如果有去重就不出问题).
    """
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "no-dup-forced")
    try:
        _enter(page, sid)
        page.wait_for_timeout(300)
        for m in ["alpha-f", "bravo-f", "charlie-f"]:
            _send(page, m)
            page.wait_for_timeout(700)
        page.wait_for_function(
            "() => document.querySelectorAll('.bubble.user').length === 3",
            timeout=5000,
        )
        # reload, 然后在 enterChat 前 wrap idbGetSessionMessages 加 600ms 延迟,
        # 保证 WS open 先于 IDB replay 拿到行.
        page.reload()
        page.wait_for_load_state("networkidle")
        page.evaluate("""
          () => {
            const orig = window.idbGetSessionMessages;
            if (!orig || orig.__patched) return;
            const patched = async (sid) => {
              await new Promise(r => setTimeout(r, 600));
              return orig(sid);
            };
            patched.__patched = true;
            window.idbGetSessionMessages = patched;
          }
        """)
        page.wait_for_selector(f"[data-id='{sid}']", timeout=10000)
        page.locator(f"[data-id='{sid}']").click()
        expect(page.locator("body")).to_have_class(
            re.compile(r"\bhas-session\b"), timeout=10000)
        page.wait_for_selector(".bubble.user", timeout=5000)
        # 给足时间让两路都跑完
        page.wait_for_timeout(2500)

        bubbles = page.evaluate(
            "[...document.querySelectorAll('.bubble.user')].map("
            "b => (b.textContent || '').trim())"
        )
        assert len(bubbles) == 3, (
            f"forced race 后应有 3 条 user bubble, 实际 {len(bubbles)}: {bubbles}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
