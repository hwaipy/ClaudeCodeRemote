"""§4 聊天视图 — chat-log 滚动位置持久化契约:

- 页面 hard refresh 后首次进入任意聊天 → scroll 到底
- 同一次页面生命周期内, 再次进入已访问过的聊天 → 恢复上次离开时的 scrollTop
- 离开 (点 ← / 滑回 / 切别的卡) → 保存当前 scrollTop
- 不同 session 互不污染
- hard refresh 清空缓存 → 回到"首次进入"语义 (滚到底)
"""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


def _enter_chat(page, sid):
    card = page.locator(f"[data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)
    card.click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    # first_paint 之后的 30-frame stickBottom settle loop ≈ 500ms.
    # 等它跑完再操作 scrollTop, 否则我们的 set 会被 rAF 覆盖.
    page.wait_for_timeout(700)


def _back_to_home(page):
    """走 chat-back click — 同样的退出路径 (滑回也会调它)."""
    page.locator("#chat-back").dispatch_event("click")
    expect(page.locator("body")).not_to_have_class(
        re.compile(r"\bhas-session\b"), timeout=5000
    )


def _pad_and_scroll_to(page, target_scroll):
    """在一个 evaluate 里 pad + set scrollTop — 避免被 rAF 抢走."""
    return page.evaluate(
        """(target) => {
          const log = document.getElementById("chat-log");
          for (let i = 0; i < 40; i++) {
            const d = document.createElement("div");
            d.style.minHeight = "60px";
            d.style.padding = "8px";
            d.textContent = "filler line " + i;
            log.appendChild(d);
          }
          const prev = log.style.scrollBehavior;
          log.style.scrollBehavior = "auto";
          log.scrollTop = target;
          log.style.scrollBehavior = prev;
          return { scrollTop: log.scrollTop, scrollHeight: log.scrollHeight,
                   clientHeight: log.clientHeight };
        }""",
        target_scroll,
    )


def _scroll_info(page):
    return page.evaluate("""
      () => {
        const log = document.getElementById("chat-log");
        return {
          scrollTop: log.scrollTop,
          scrollHeight: log.scrollHeight,
          clientHeight: log.clientHeight,
        };
      }
    """)


def test_exit_and_return_restores_scroll_position(
    logged_in_page, base_url, test_token
):
    """关键契约 (用户报的 bug): 进入聊天 → 滚到中间 → 退出 → 再进入 →
    scrollTop 必须恢复到上次离开时的值, 不能跳回顶部."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "scroll-restore")
    try:
        _enter_chat(page, sid)
        info_before = _pad_and_scroll_to(page, 300)
        assert info_before["scrollTop"] == 300, (
            f"setup failed: expected scrollTop=300, got {info_before}"
        )

        _back_to_home(page)

        _enter_chat(page, sid)
        info_after = _scroll_info(page)
        assert info_after["scrollTop"] == 300, (
            f"scroll position must be restored after exit/return; "
            f"expected 300, got {info_after}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_different_sessions_have_independent_scroll(
    logged_in_page, base_url, test_token
):
    page = logged_in_page
    a = api_spawn(base_url, test_token, "/tmp", "scroll-a")
    b = api_spawn(base_url, test_token, "/tmp", "scroll-b")
    try:
        # A: 滚 300
        _enter_chat(page, a)
        info_a_set = _pad_and_scroll_to(page, 300)
        assert info_a_set["scrollTop"] == 300
        _back_to_home(page)
        # B: 滚 800
        _enter_chat(page, b)
        info_b_set = _pad_and_scroll_to(page, 800)
        assert info_b_set["scrollTop"] == 800
        _back_to_home(page)
        # 回 A: 应该还是 300
        _enter_chat(page, a)
        info_a = _scroll_info(page)
        assert info_a["scrollTop"] == 300, (
            f"session A's scroll must be independent of B's; got {info_a}"
        )
        _back_to_home(page)
        # 回 B: 应该还是 800
        _enter_chat(page, b)
        info_b = _scroll_info(page)
        assert info_b["scrollTop"] == 800, (
            f"session B's scroll must be independent of A's; got {info_b}"
        )
    finally:
        api_delete_session(base_url, test_token, a)
        api_delete_session(base_url, test_token, b)


def test_scroll_to_bottom_button_shows_on_reenter_when_not_at_bottom(
    logged_in_page, base_url, test_token
):
    """用户报的契约: 只要聊天不在最底部, ↓ 按钮必须可见. 包括从 home 切
    回聊天 (cache hit) 时, 如果恢复的 scrollTop 不在底部, 按钮必须立刻
    可见, 不需要用户手动滚一下才触发."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "scroll-arrow")
    try:
        _enter_chat(page, sid)
        info = _pad_and_scroll_to(page, 300)
        assert info["scrollTop"] == 300
        # 在 mid-scroll 状态下, ↓ 按钮已经可见 (scroll 事件触发的 sync)
        btn_hidden_mid = page.evaluate(
            "() => document.getElementById('scroll-to-bottom').hidden"
        )
        assert not btn_hidden_mid, "↓ button must be visible mid-scroll"

        _back_to_home(page)
        _enter_chat(page, sid)
        # cache hit: scrollTop 恢复 300; ↓ 按钮必须立刻可见
        page.wait_for_timeout(100)
        btn_hidden_after = page.evaluate(
            "() => document.getElementById('scroll-to-bottom').hidden"
        )
        assert not btn_hidden_after, (
            "↓ button must be visible immediately after re-entering chat "
            "with restored mid-scroll position"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_scroll_to_bottom_button_appears_when_content_grows_past_view(
    logged_in_page, base_url, test_token
):
    """关键场景: 首次进入聊天 (cache miss), chat-log 由空变满, scrollTop
    保持在 0 (没人自动滚底), distFromBottom 应该 > 阈值, ↓ 按钮必须立刻
    可见. 之前 syncScrollToBottomBtn 只在 scroll 事件触发, content 长大
    但 scrollTop 不变时按钮不出现 — 这是 bug."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "scroll-grow")
    try:
        _enter_chat(page, sid)
        page.wait_for_timeout(700)   # 让 first_paint 的 settle 走完
        # 重置 chat-log 到空 + scrollTop=0, 模拟首次进入的初始态
        page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            log.scrollTop = 0;
          }
        """)
        page.wait_for_timeout(100)
        # 验证起点: 按钮隐藏 (chat 空 = atBottom)
        hidden_at_start = page.evaluate(
            "() => document.getElementById('scroll-to-bottom').hidden"
        )
        assert hidden_at_start, "empty chat-log: button should be hidden"

        # 模拟内容流入 chat-log (不动 scrollTop) — distFromBottom 会变大
        page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            for (let i = 0; i < 40; i++) {
              const d = document.createElement('div');
              d.style.minHeight = '60px';
              d.textContent = 'msg ' + i;
              log.appendChild(d);
            }
          }
        """)
        page.wait_for_timeout(200)   # 给 MutationObserver 一点时间
        hidden_after_grow = page.evaluate(
            "() => document.getElementById('scroll-to-bottom').hidden"
        )
        assert not hidden_after_grow, (
            "↓ button must appear when content overflows chat-log even if "
            "scrollTop didn't change (need MutationObserver / similar to "
            "trigger sync on DOM growth)"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_hard_refresh_clears_in_memory_cache(
    logged_in_page, base_url, test_token
):
    """hard refresh 清空 in-memory sessionCache → 下次进入回到 "首次" 语义.
    无法在测试里模拟 chat 内容的真实 first_paint 滚底, 但验证 cache 清空
    即足以让 cache-miss 分支生效."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "scroll-refresh")
    try:
        _enter_chat(page, sid)
        _pad_and_scroll_to(page, 300)
        _back_to_home(page)
        has_cache_before = page.evaluate(
            f"() => state.sessionCache.has('{sid}')"
        )
        assert has_cache_before, "saveCurrentSessionCache should have cached this"

        page.reload()
        page.wait_for_selector("#view-home.active", timeout=5000)

        has_cache_after = page.evaluate(
            f"() => state.sessionCache.has('{sid}')"
        )
        assert not has_cache_after, (
            "hard refresh must clear in-memory sessionCache, so next entry "
            "follows the cache-miss path"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
