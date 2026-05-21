"""§4 聊天视图 - 收发新消息时的滚动行为:

- 用户在底部 (距底 < 40 px) → 新消息到达时, scrollTop 跟随到新底部
- 用户已经滚走 (距底 >= 40 px) → 新消息到达时, scrollTop **不变**, 不抢用户阅读位置
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
    page.wait_for_timeout(700)   # let first_paint settle loop finish


def _pad_and_scroll_to(page, target):
    return page.evaluate(
        """(target) => {
          const log = document.getElementById("chat-log");
          for (let i = 0; i < 40; i++) {
            const d = document.createElement("div");
            d.style.minHeight = "60px";
            d.textContent = "msg " + i;
            log.appendChild(d);
          }
          const prev = log.style.scrollBehavior;
          log.style.scrollBehavior = "auto";
          log.scrollTop = target;
          log.style.scrollBehavior = prev;
          return { scrollTop: log.scrollTop, scrollHeight: log.scrollHeight,
                   clientHeight: log.clientHeight };
        }""",
        target,
    )


def _inject_new_msg_and_trigger_autoscroll(page):
    """模拟新消息: appendBubble + chatScrollBottom() 调用. chat-log 默认
    scroll-behavior: smooth, 所以等 500 ms 让动画落定再读 scrollTop."""
    page.evaluate("""
      () => {
        const log = document.getElementById("chat-log");
        window.__stickTest = {
          prevTop: log.scrollTop,
          prevHeight: log.scrollHeight,
        };
        const d = document.createElement("div");
        d.style.minHeight = "60px";
        d.textContent = "NEW msg";
        log.appendChild(d);
        if (typeof chatScrollBottom === "function") chatScrollBottom();
      }
    """)
    page.wait_for_timeout(500)
    return page.evaluate("""
      () => {
        const log = document.getElementById("chat-log");
        return {
          ...window.__stickTest,
          newTop: log.scrollTop,
          newHeight: log.scrollHeight,
          newClient: log.clientHeight,
        };
      }
    """)


def test_at_bottom_then_new_msg_scrolls_to_new_bottom(
    logged_in_page, base_url, test_token
):
    """用户在底部, 新消息到 → scrollTop 跟随到新底部."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "stick-bottom")
    try:
        _enter_chat(page, sid)
        # 先 pad 并直接滚到底
        info = page.evaluate("""
          () => {
            const log = document.getElementById("chat-log");
            for (let i = 0; i < 40; i++) {
              const d = document.createElement("div");
              d.style.minHeight = "60px";
              d.textContent = "msg " + i;
              log.appendChild(d);
            }
            const prev = log.style.scrollBehavior;
            log.style.scrollBehavior = "auto";
            log.scrollTop = log.scrollHeight;   // 到底
            log.style.scrollBehavior = prev;
            return { scrollTop: log.scrollTop, scrollHeight: log.scrollHeight,
                     clientHeight: log.clientHeight };
          }
        """)
        # 确认是 at-bottom
        dist = info["scrollHeight"] - info["scrollTop"] - info["clientHeight"]
        assert dist < 5, f"setup: should be at bottom, dist={dist}"
        # Wait a moment so the scroll listener updates state.atBottom
        page.wait_for_timeout(150)

        result = _inject_new_msg_and_trigger_autoscroll(page)
        # 新消息后, 应该粘到新底部
        dist_after = (result["newHeight"]
                      - result["newTop"]
                      - result["newClient"])
        assert dist_after < 5, (
            f"at-bottom + new msg should stick to new bottom; "
            f"prev top={result['prevTop']}, new top={result['newTop']}, "
            f"dist_after={dist_after}, result={result}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_scrolled_up_then_new_msg_does_not_steal_scroll(
    logged_in_page, base_url, test_token
):
    """用户滚到中间, 新消息到 → scrollTop 不变 (不抢阅读位置)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "stick-noop")
    try:
        _enter_chat(page, sid)
        info = _pad_and_scroll_to(page, 300)
        assert info["scrollTop"] == 300
        # 让 scroll 监听器更新 state.atBottom = false
        page.wait_for_timeout(150)

        result = _inject_new_msg_and_trigger_autoscroll(page)
        assert result["newTop"] == 300, (
            f"scrolled up + new msg must NOT move scrollTop; "
            f"expected 300, got {result['newTop']}, result={result}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
