"""chat-input Enter 键行为 — 统一: Enter 发送, Shift+Enter 换行.

桌面 + 移动一致 (ChatGPT/Slack 风格). 之前曾尝试过桌面 Enter=换行 /
Ctrl+Enter=发送 (v189), 实测改变了用户肌肉记忆, v191 撤回."""
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
    return page.evaluate(f"""
      () => {{
        const ta = document.getElementById('chat-input');
        ta.focus();
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
        window.sendUserMessage = window.__origSend;
        return {{
          sent: sent,
          preventedDefault: preventedDefault,
          value: ta.value,
        }};
      }}
    """)


def test_desktop_enter_sends(wide_page, base_url, test_token):
    """桌面 Enter (无 shift) → 触发 send + preventDefault."""
    page = wide_page
    sid = api_spawn(base_url, test_token, "/tmp", "desktop-enter")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-input").fill("hello")
        r = _press_key(page, "Enter")
        assert r["sent"], f"桌面 Enter 应触发 send, 实际 {r}"
        assert r["preventedDefault"], "桌面 Enter 应 preventDefault"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_desktop_shift_enter_does_not_send(wide_page, base_url, test_token):
    """桌面 Shift+Enter → 不触发 send, 不 preventDefault (textarea 自然换行)."""
    page = wide_page
    sid = api_spawn(base_url, test_token, "/tmp", "desktop-shift-enter")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-input").fill("hi")
        r = _press_key(page, "Enter", modifiers=["shift"])
        assert not r["sent"], f"桌面 Shift+Enter 不该触发 send, 实际 {r}"
        assert not r["preventedDefault"], (
            "桌面 Shift+Enter 不该 preventDefault (要让 textarea 换行)"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_mobile_enter_sends(browser, base_url, test_token):
    """触屏 (mobile context) Enter 同样触发 send."""
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
