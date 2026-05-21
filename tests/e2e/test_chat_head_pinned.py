"""§4 聊天视图 — #chat-head 固定契约:

- computed position 必须是 'fixed', z-index > #chat-log
- 滚动 #chat-log 时 head 的 viewport 坐标完全不动
- 模拟 visualViewport.offsetTop 变化 (iOS 键盘弹出) → head.style.top 跟随
- focusin/focusout 上启动 ~800ms 的 rAF pin loop (源码白盒)
"""
from __future__ import annotations

import pathlib
import re

import pytest
from playwright.sync_api import expect


def _enter_chat(page, sid):
    """Navigate to chat view for the given session via direct DOM click."""
    card = page.locator(f"[data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)
    card.click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )


def test_chat_head_computed_position_is_fixed(logged_in_page, spawned_session):
    sid = spawned_session(name="chat-head-pos")
    page = logged_in_page
    _enter_chat(page, sid)
    pos = page.evaluate(
        "() => getComputedStyle(document.getElementById('chat-head')).position"
    )
    assert pos == "fixed", f"chat-head must be position:fixed, got {pos!r}"
    z = page.evaluate(
        "() => parseInt(getComputedStyle(document.getElementById('chat-head'))"
        ".zIndex, 10)"
    )
    log_z = page.evaluate(
        "() => parseInt(getComputedStyle(document.getElementById('chat-log'))"
        ".zIndex, 10) || 0"
    )
    assert z > log_z, (
        f"chat-head z-index ({z}) must be > chat-log z-index ({log_z})"
    )


def test_chat_head_does_not_move_when_chat_log_scrolls(
    logged_in_page, spawned_session
):
    """滚动 chat-log → chat-head 在 viewport 坐标里完全不动."""
    sid = spawned_session(name="chat-head-scroll")
    page = logged_in_page
    _enter_chat(page, sid)

    # Stuff some content so chat-log is scrollable. Fake by injecting
    # placeholder bubbles directly into the DOM (the test isn't about
    # real messages; it's about scroll behavior).
    page.evaluate("""
      () => {
        const log = document.getElementById("chat-log");
        for (let i = 0; i < 30; i++) {
          const div = document.createElement("div");
          div.style.minHeight = "80px";
          div.style.background = i % 2 ? "#0001" : "transparent";
          div.textContent = "filler " + i;
          log.appendChild(div);
        }
      }
    """)
    page.wait_for_timeout(80)

    head_before = page.locator("#chat-head").bounding_box()
    assert head_before
    # Programmatically scroll chat-log down.
    page.evaluate(
        "() => { document.getElementById('chat-log').scrollTop = 600; }"
    )
    page.wait_for_timeout(100)
    head_after = page.locator("#chat-head").bounding_box()
    assert head_after
    dy = abs(head_after["y"] - head_before["y"])
    dx = abs(head_after["x"] - head_before["x"])
    assert dy <= 1 and dx <= 1, (
        f"chat-head should NOT move on chat-log scroll: "
        f"before={head_before}, after={head_after}, dy={dy}, dx={dx}"
    )


def _narrow_page(playwright, base_url, test_token):
    """Helper: launch a fresh narrow-viewport browser context (mobile-like)."""
    browser = playwright.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True, is_mobile=True,
    )
    page = ctx.new_page()
    page.goto(base_url)
    page.fill("#login-token", test_token)
    page.click("#login-go")
    page.wait_for_selector("#view-home.active", timeout=5000)
    return browser, ctx, page


def test_narrow_chat_head_stays_in_dom_when_no_session(
    playwright, base_url, test_token
):
    """新契约: 窄屏下 chat-head 始终 display:flex, 用 transform 控制可见性,
    没有 session 时 transform: translateX(100%) (但 display 不是 none)."""
    browser, ctx, page = _narrow_page(playwright, base_url, test_token)
    try:
        # No has-session yet — chat-head MUST be display:flex but transformed off
        disp = page.evaluate(
            "() => getComputedStyle(document.getElementById('chat-head')).display"
        )
        assert disp == "flex", f"narrow chat-head must be display:flex, got {disp!r}"
        # transform should NOT be 'none' — it must be a non-identity matrix
        transform = page.evaluate(
            "() => getComputedStyle(document.getElementById('chat-head'))"
            ".transform"
        )
        assert transform != "none" and "matrix" in transform, (
            f"narrow chat-head must have a transform when no session: {transform}"
        )
    finally:
        ctx.close(); browser.close()


def _parse_transitions(prop, dur, fn):
    """getComputedStyle 把多条 transition 拆成 csv 字符串. 解析成
    {property: (duration, timingFunction)} dict."""
    props = [p.strip() for p in prop.split(",")]
    durs = [d.strip() for d in dur.split(",")]
    fns = [f.strip() for f in fn.split(",")]
    return {props[i]: (durs[i], fns[i]) for i in range(len(props))}


def test_narrow_chat_head_and_view_chat_share_transform_transition(
    playwright, base_url, test_token
):
    """`transform` 的过渡时序必须一致 — 否则滑入/滑出动画两片错位."""
    browser, ctx, page = _narrow_page(playwright, base_url, test_token)
    try:
        head_ts = page.evaluate("""
          () => {
            const cs = getComputedStyle(document.getElementById('chat-head'));
            return { prop: cs.transitionProperty, dur: cs.transitionDuration,
                     fn: cs.transitionTimingFunction };
          }
        """)
        view_ts = page.evaluate("""
          () => {
            document.body.classList.add('stage-app');
            const cs = getComputedStyle(document.getElementById('view-chat'));
            return { prop: cs.transitionProperty, dur: cs.transitionDuration,
                     fn: cs.transitionTimingFunction };
          }
        """)
        head_map = _parse_transitions(head_ts["prop"], head_ts["dur"], head_ts["fn"])
        view_map = _parse_transitions(view_ts["prop"], view_ts["dur"], view_ts["fn"])
        assert "transform" in head_map, f"chat-head must transition transform: {head_ts}"
        assert "transform" in view_map, f"view-chat must transition transform: {view_ts}"
        assert head_map["transform"] == view_map["transform"], (
            f"transform transition mismatch: "
            f"head={head_map['transform']}, view={view_map['transform']}"
        )
    finally:
        ctx.close(); browser.close()


def test_swipe_back_drives_head_with_view(
    playwright, base_url, test_token
):
    """白盒: installSwipeBack 在 view-chat 上必须用 followIds:['chat-head']
    把 head 也推进同一 transform 流程."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    # Find the chat-view swipe install call
    m = re.search(
        r'installSwipeBack\(\s*"view-chat"\s*,\s*\{(.*?)\}\s*\)',
        src, flags=re.S,
    )
    assert m, "installSwipeBack('view-chat', ...) call not found"
    block = m.group(1)
    assert '"chat-head"' in block and "followIds" in block, (
        f"view-chat swipe must list chat-head in followIds, got: {block}"
    )


def test_chat_head_hides_when_chat_input_focused_on_narrow(
    playwright, base_url, test_token
):
    """选项 C: 窄屏聚焦 chat-input → chat-head display:none. 这是用户
    选定的方案 — 键盘期间 head 不存在, 不会被 iOS layout viewport 推动."""
    browser = playwright.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True, is_mobile=True,
    )
    page = ctx.new_page()
    try:
        page.goto(base_url)
        page.fill("#login-token", test_token)
        page.click("#login-go")
        page.wait_for_selector("#view-home.active", timeout=5000)

        # Spawn a session via API and enter chat
        from tests.helpers import api_spawn, api_delete_session
        sid = api_spawn(base_url, test_token, "/tmp", "head-hide-focus")
        try:
            page.locator(f"[data-id='{sid}']").click()
            page.wait_for_selector("body.has-session", timeout=5000)
            page.wait_for_timeout(500)   # let slide-in animation settle

            # Initially head visible
            disp_before = page.evaluate(
                "() => getComputedStyle(document.getElementById("
                "'chat-head')).display"
            )
            assert disp_before == "flex", (
                f"head should be visible before focus, got {disp_before}"
            )

            # Focus the textarea — head must disappear
            page.locator("#chat-input").focus()
            page.wait_for_timeout(50)
            disp_after = page.evaluate(
                "() => getComputedStyle(document.getElementById("
                "'chat-head')).display"
            )
            assert disp_after == "none", (
                f"head must hide on chat-input focus, got display={disp_after}"
            )

            # Blur — head returns
            page.evaluate("() => document.getElementById('chat-input').blur()")
            page.wait_for_timeout(50)
            disp_back = page.evaluate(
                "() => getComputedStyle(document.getElementById("
                "'chat-head')).display"
            )
            assert disp_back == "flex", (
                f"head must return after blur, got display={disp_back}"
            )
        finally:
            api_delete_session(base_url, test_token, sid)
    finally:
        ctx.close(); browser.close()


def test_chat_head_does_NOT_hide_on_focus_in_wide_layout(
    logged_in_page, spawned_session
):
    """宽屏 (>=900px) 不应该隐藏 head — 双栏布局没有滑动键盘问题,
    head 应该一直可见."""
    sid = spawned_session(name="head-stays-wide")
    page = logged_in_page
    _enter_chat(page, sid)
    page.locator("#chat-input").focus()
    page.wait_for_timeout(50)
    disp = page.evaluate(
        "() => getComputedStyle(document.getElementById('chat-head')).display"
    )
    assert disp == "flex", (
        f"wide-screen chat-head must remain visible on input focus, got {disp}"
    )


