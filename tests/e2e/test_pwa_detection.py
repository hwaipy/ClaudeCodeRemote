"""§4 PWA / Safari tab 检测: JS 起手把 PWA standalone 检测结果反映到
`body.is-pwa` class. CSS 用这个 class 做 PWA-only 微调 (例如键盘期间
收紧 chat-foot padding-bottom 避免大下巴)."""
from __future__ import annotations

import re
import pathlib


def test_js_detects_pwa_mode():
    """白盒: JS 必须组合 matchMedia + navigator.standalone 双重检测."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "matchMedia(\"(display-mode: standalone)\")" in src \
        or "matchMedia('(display-mode: standalone)')" in src, (
        "PWA detection must use matchMedia('(display-mode: standalone)')"
    )
    assert "navigator.standalone" in src, (
        "PWA detection must also check iOS-specific navigator.standalone"
    )
    assert 'classList.add("is-pwa")' in src or "classList.add('is-pwa')" in src, (
        "PWA detection must add `body.is-pwa` class when in standalone mode"
    )


def test_css_pwa_chat_foot_padding_override():
    """白盒: PWA + chat-input 获焦时收紧 chat-foot padding-bottom (避免
    iOS PWA 键盘期间 home indicator env 报值过大导致的下巴)."""
    css = pathlib.Path(
        "claude_code_remote/server/static/style.css"
    ).read_text()
    # 必须有 PWA-only override
    assert re.search(
        r"body\.is-pwa[^{]*#chat-input:focus[^{]*\.chat-foot\s*\{[^}]*padding-bottom",
        css, re.S,
    ) or re.search(
        r"body\.is-pwa\.has-session:has\(#chat-input:focus\) \.chat-foot",
        css,
    ), "CSS must scope the chat-foot padding-bottom override to body.is-pwa"


def test_runtime_body_has_is_pwa_class_when_in_pwa_context(
    playwright, base_url, test_token
):
    """模拟 PWA: 用 chromium 的 isMobile + custom UA 启动一个新 context
    并 emulate matchMedia 把 display-mode 设成 standalone, 然后 reload 主
    页. 期望 body.classList 包含 'is-pwa'."""
    browser = playwright.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True, is_mobile=True,
    )
    # 注入 init script: 模拟 iOS PWA standalone (navigator.standalone=true).
    # 在每个页面加载前装好, 我们的 _IS_PWA 检测会拿到 true.
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'standalone', "
        "{ value: true, configurable: true });"
    )
    page = ctx.new_page()
    try:
        page.goto(base_url)
        page.fill("#login-token", test_token)
        page.click("#login-go")
        page.wait_for_selector("#view-home.active", timeout=5000)
        # 先检查 matchMedia 是否真的报告 standalone (诊断信息)
        info = page.evaluate("""
          () => ({
            mediaMatch: window.matchMedia('(display-mode: standalone)').matches,
            navStand: window.navigator.standalone,
            hasClass: document.body.classList.contains('is-pwa'),
          })
        """)
        assert info["mediaMatch"] or info["navStand"], (
            f"CDP emulation didn't enable PWA media: {info}"
        )
        assert info["hasClass"], (
            f"body must have 'is-pwa' class when standalone matches: {info}"
        )
    finally:
        ctx.close(); browser.close()


def test_runtime_body_lacks_is_pwa_class_in_normal_tab(logged_in_page):
    """对照: 普通 chromium tab (无 display-mode override) 不应该有 .is-pwa."""
    has_class = logged_in_page.evaluate(
        "() => document.body.classList.contains('is-pwa')"
    )
    assert not has_class, (
        "body must NOT have 'is-pwa' class in regular browser tab"
    )
