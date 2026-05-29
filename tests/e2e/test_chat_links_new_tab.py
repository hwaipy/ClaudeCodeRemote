"""聊天里渲染的外链 (http/https) 点击必须在新 tab / 外部打开, 不在当前
PWA 里导航走. 用全局 delegated handler 强制 window.open(_blank)."""
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


def test_handler_present():
    """白盒: 有 delegated click handler 拦外链 window.open(_blank)."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert 'window.open(href, "_blank"' in src, (
        "应有 window.open(_blank) 外链拦截"
    )
    assert "closest(\"a[href]\")" in src


def test_external_link_opens_new_tab(logged_in_page, base_url, test_token):
    """渲一条带外链的 assistant 消息 → 点链接 → 触发 popup (window.open),
    且当前页 URL 不变 (没被导航走)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "link-newtab")
    try:
        _enter_chat(page, sid)
        # 注入一条含 markdown 链接的 assistant 消息
        page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            handleAssistantMessage({
              id: 'msg_link',
              content: [
                { type: 'text',
                  text: 'see [example](https://example.com/path) and '
                        + 'bare https://example.org/bare' },
              ],
            });
          }
        """)
        link = page.locator("#chat-log a[href='https://example.com/path']")
        expect(link).to_be_visible(timeout=2000)
        # 链接应带 target=_blank (sanitizeMD) — 双保险
        assert link.get_attribute("target") == "_blank"

        url_before = page.url
        # 点击应触发 popup (window.open), 不导航当前页
        with page.context.expect_page() as popup_info:
            link.click()
        popup = popup_info.value
        assert "example.com/path" in popup.url, (
            f"popup 应打开点击的外链, got {popup.url}"
        )
        popup.close()
        assert page.url == url_before, (
            f"当前 PWA 页不应被导航走: {url_before} → {page.url}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_internal_hash_link_not_intercepted(
    logged_in_page, base_url, test_token
):
    """站内 #hash / 相对链接不被拦 (例如 OAuth start 链接 / #help)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "link-hash")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            // 模拟点一个 #hash 链接, 不应被 window.open 拦
            let opened = null;
            const origOpen = window.open;
            window.open = (u) => { opened = u; return null; };
            const a = document.createElement('a');
            a.href = '#help';
            document.body.appendChild(a);
            a.click();
            window.open = origOpen;
            a.remove();
            return { opened };
          }
        """)
        assert result["opened"] is None, (
            f"#hash 链接不该触发 window.open, got {result['opened']!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
