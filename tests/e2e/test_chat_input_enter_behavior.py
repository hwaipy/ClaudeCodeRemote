"""chat-input Enter 键行为 — 按 primary pointer 类型分流.

桌面 (pointer: fine): Enter 换行, Ctrl/⌘+Enter 发送.
触屏 (pointer: coarse): Enter 发送 (soft keyboard 没 Ctrl 组合).

测试用 page.evaluate 注入伪造 keydown event 验证 handler 分支, 不依赖
真实键盘行为 (playwright 难精确模拟"换行"vs"发送"). 真实用户体验靠
人工 + spec mockup 对照."""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    card = page.locator(f"[data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)
    card.click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=10000)


def _press_key(page, key, modifiers=None):
    """注入 keydown 到 chat-input. 返回 {sent: bool, value: str} —
    sent 通过 hook sendUserMessage 标志位; value 是事件结束后 textarea 值."""
    return page.evaluate(f"""
      () => {{
        const ta = document.getElementById('chat-input');
        ta.focus();
        // hook 一下 sendUserMessage 检测是否被触发
        if (!window.__origSend) window.__origSend = window.sendUserMessage;
        let sent = false;
        window.sendUserMessage = () => {{ sent = true; }};
        const ev = new KeyboardEvent('keydown', {{
          key: '{key}',
          ctrlKey: {('true' if modifiers and 'ctrl' in modifiers else 'false')},
          metaKey: {('true' if modifiers and 'meta' in modifiers else 'false')},
          shiftKey: {('true' if modifiers and 'shift' in modifiers else 'false')},
          bubbles: true, cancelable: true,
        }});
        const preventedDefault = !ta.dispatchEvent(ev);
        // 还原
        window.sendUserMessage = window.__origSend;
        return {{
          sent: sent,
          preventedDefault: preventedDefault,
          value: ta.value,
        }};
      }}
    """)


def test_desktop_enter_does_not_send(wide_page, base_url, test_token):
    """桌面 (wide_page, pointer:fine): Enter 不触发 sendUserMessage, 也不
    preventDefault → textarea 自然换行."""
    page = wide_page
    sid = api_spawn(base_url, test_token, "/tmp", "desktop-enter")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-input").fill("hello")
        r = _press_key(page, "Enter")
        assert not r["sent"], (
            f"桌面 Enter 不该触发 send, 实际 {r}"
        )
        assert not r["preventedDefault"], (
            "桌面 Enter 不该 preventDefault (要让 textarea 自然插换行)"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_desktop_ctrl_enter_sends(wide_page, base_url, test_token):
    """桌面 Ctrl+Enter 触发 send + preventDefault."""
    page = wide_page
    sid = api_spawn(base_url, test_token, "/tmp", "desktop-ctrl-enter")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-input").fill("hi")
        r = _press_key(page, "Enter", modifiers=["ctrl"])
        assert r["sent"], f"桌面 Ctrl+Enter 应触发 send, 实际 {r}"
        assert r["preventedDefault"], "桌面 Ctrl+Enter 应 preventDefault"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_desktop_meta_enter_sends(wide_page, base_url, test_token):
    """桌面 ⌘+Enter (Mac) 也触发 send."""
    page = wide_page
    sid = api_spawn(base_url, test_token, "/tmp", "desktop-meta-enter")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-input").fill("hi")
        r = _press_key(page, "Enter", modifiers=["meta"])
        assert r["sent"], f"桌面 ⌘+Enter 应触发 send, 实际 {r}"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_mobile_enter_sends(browser, base_url, test_token):
    """触屏 (mobile_page, pointer:coarse) 下 Enter 仍触发 send."""
    # 显式建一个 has_touch=True + 触屏 pointer 的 context
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True, is_mobile=True,
    )
    ctx.add_init_script(
        f"try {{ localStorage.setItem('ccr.token', {test_token!r}); }} catch (e) {{}}"
    )
    page = ctx.new_page()
    page.goto(base_url)
    sid = api_spawn(base_url, test_token, "/tmp", "mobile-enter")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-input").fill("hello")
        r = _press_key(page, "Enter")
        assert r["sent"], f"移动端 Enter 应触发 send, 实际 {r}"
        assert r["preventedDefault"], "移动端 Enter 应 preventDefault"
    finally:
        api_delete_session(base_url, test_token, sid)
        ctx.close()
