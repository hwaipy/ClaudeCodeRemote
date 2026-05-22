"""§6 聊天输入框草稿持久化:

- 每次键入 → localStorage 立即写 (随时持久化, 应对闪退)
- 每个 session 独立 key (ccr.draft.<sid>)
- 切到别的 session 不污染当前文字
- 切回原 session 自动恢复
- page.reload() 模拟闪退 → 再进 session 草稿恢复
- 发送消息后 → key 清除
- tmp session (前端 quick-new) 不持久化
"""
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
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
    page.wait_for_timeout(200)


def _back_home(page):
    page.locator("#chat-back").dispatch_event("click")
    expect(page.locator("body")).not_to_have_class(
        re.compile(r"\bhas-session\b"), timeout=5000
    )


def _draft_key_value(page, sid):
    return page.evaluate(
        "(sid) => localStorage.getItem('ccr.draft.' + sid)", sid,
    )


# ---------- 白盒 ----------

def test_input_handler_writes_localstorage():
    """chat-input 'input' 事件 listener 必须 setItem localStorage."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    # 找 chat-input 的 input handler
    m = re.search(
        r'\$\("chat-input"\)\.addEventListener\("input",[^{]*\{(.*?)\}\s*\)',
        src, re.S,
    )
    assert m, "chat-input 'input' handler not found"
    body = m.group(1)
    assert 'localStorage.setItem' in body, (
        "input handler 必须 localStorage.setItem (随时持久化)"
    )
    assert 'ccr.draft.' in body, (
        "key 必须用 'ccr.draft.<sid>' 命名约定"
    )


def test_send_clears_draft_key():
    """发送 user_message 路径必须 removeItem(ccr.draft.<sid>)."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert src.count('localStorage.removeItem("ccr.draft.') >= 1, (
        "send 路径需要清除该 session 的 draft key"
    )


# ---------- 运行时 ----------

def test_typing_writes_localstorage_immediately(
    logged_in_page, base_url, test_token,
):
    """每按一个键 localStorage 都同步更新 — 真'随时持久化'."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "draft-rt")
    try:
        _enter_chat(page, sid)
        ta = page.locator("#chat-input")
        # 没输入时 key 不存在
        assert _draft_key_value(page, sid) is None
        # 输 "h" → localStorage 立即有 "h"
        ta.focus()
        page.keyboard.type("h")
        assert _draft_key_value(page, sid) == "h"
        # 继续输 "ello" → 每按一次 key 都该是当前完整文字
        page.keyboard.type("e")
        assert _draft_key_value(page, sid) == "he"
        page.keyboard.type("llo")
        assert _draft_key_value(page, sid) == "hello"
        # 全清 → key 被 removeItem
        ta.evaluate(
            "el => { el.value = ''; el.dispatchEvent(new Event('input')); }"
        )
        assert _draft_key_value(page, sid) is None
    finally:
        api_delete_session(base_url, test_token, sid)


def test_drafts_independent_per_session(
    logged_in_page, base_url, test_token,
):
    """A 输 x → 切到 B → B textarea 空, A 的 draft 仍存."""
    page = logged_in_page
    sid_a = api_spawn(base_url, test_token, "/tmp", "draft-a")
    sid_b = api_spawn(base_url, test_token, "/tmp", "draft-b")
    try:
        _enter_chat(page, sid_a)
        page.locator("#chat-input").focus()
        page.keyboard.type("draft for A")
        assert _draft_key_value(page, sid_a) == "draft for A"
        _back_home(page)
        _enter_chat(page, sid_b)
        # B 的 textarea 应该为空
        assert page.locator("#chat-input").input_value() == ""
        # A 的草稿仍在 localStorage
        assert _draft_key_value(page, sid_a) == "draft for A"
        assert _draft_key_value(page, sid_b) is None
    finally:
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)


def test_draft_restored_on_switch_back(
    logged_in_page, base_url, test_token,
):
    """A 输 → 切 B → 回 A: textarea 自动恢复草稿."""
    page = logged_in_page
    sid_a = api_spawn(base_url, test_token, "/tmp", "draft-switch-a")
    sid_b = api_spawn(base_url, test_token, "/tmp", "draft-switch-b")
    try:
        _enter_chat(page, sid_a)
        page.locator("#chat-input").focus()
        page.keyboard.type("my draft")
        _back_home(page)
        _enter_chat(page, sid_b)
        assert page.locator("#chat-input").input_value() == ""
        _back_home(page)
        _enter_chat(page, sid_a)
        assert page.locator("#chat-input").input_value() == "my draft"
    finally:
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)


def test_draft_survives_page_reload(
    logged_in_page, base_url, test_token,
):
    """模拟闪退: 输文字 → page.reload() → 进同 session, textarea 文字回来."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "draft-reload")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-input").focus()
        page.keyboard.type("survives crash")
        assert _draft_key_value(page, sid) == "survives crash"
        # 模拟闪退 — hard reload
        page.reload()
        expect(page.locator(f"[data-id='{sid}']")).to_be_visible(timeout=10000)
        _enter_chat(page, sid)
        assert page.locator("#chat-input").input_value() == "survives crash"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_draft_cleared_after_send(
    logged_in_page, base_url, test_token,
):
    """送出 user_message → localStorage key 移除, textarea 清空."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "draft-send")
    try:
        _enter_chat(page, sid)
        ta = page.locator("#chat-input")
        ta.focus()
        page.keyboard.type("hello world")
        assert _draft_key_value(page, sid) == "hello world"
        # 直接调 sendUserMessage (避免 fake_claude 异步) 触发 send 路径
        page.evaluate("sendUserMessage()")
        page.wait_for_timeout(300)
        assert _draft_key_value(page, sid) is None, (
            "send 后 draft key 必须 removeItem"
        )
        assert page.locator("#chat-input").input_value() == ""
    finally:
        api_delete_session(base_url, test_token, sid)


def test_tmp_session_draft_not_persisted(
    logged_in_page, base_url, test_token,
):
    """tmp- 前缀 session (quick-new 还没真 spawn) — 不写 localStorage."""
    page = logged_in_page
    # 直接在 page 内模拟 tmp- session active 状态
    result = page.evaluate("""
      () => {
        // 模拟 quick-new state — sessionId 设 tmp-xxx
        state.sessionId = 'tmp-abc123';
        const ta = document.getElementById('chat-input');
        ta.value = 'tmp content';
        ta.dispatchEvent(new Event('input'));
        return localStorage.getItem('ccr.draft.tmp-abc123');
      }
    """)
    assert result is None, (
        "tmp session draft 不应写 localStorage: got " + repr(result)
    )
