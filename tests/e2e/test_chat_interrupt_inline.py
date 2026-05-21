"""§4 - #chat-interrupt 移动到 .chat-input-wrap 内, 浮在 textarea
右下角. 这样键盘弹起 head 隐藏时停止按钮仍可用."""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    page.wait_for_timeout(300)


def test_chat_interrupt_is_inside_chat_input_wrap(
    logged_in_page, base_url, test_token
):
    sid = api_spawn(base_url, test_token, "/tmp", "interrupt-inline")
    try:
        _enter_chat(logged_in_page, sid)
        is_inside = logged_in_page.evaluate("""
          () => {
            const btn = document.getElementById('chat-interrupt');
            const wrap = document.querySelector('.chat-input-wrap');
            return !!(btn && wrap && wrap.contains(btn));
          }
        """)
        assert is_inside, (
            "#chat-interrupt must be inside .chat-input-wrap (textarea sibling), "
            "not in #chat-head"
        )


    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_interrupt_vertically_centered_in_single_line_textarea(
    logged_in_page, base_url, test_token
):
    """单行 textarea (~36px) 时, 按钮垂直方向必须居中, 不能贴底."""
    sid = api_spawn(base_url, test_token, "/tmp", "interrupt-vcenter")
    try:
        _enter_chat(logged_in_page, sid)
        info = logged_in_page.evaluate("""
          () => {
            const btn = document.getElementById('chat-interrupt');
            btn.hidden = false;
            const ta = document.getElementById('chat-input');
            return {
              btnRect: btn.getBoundingClientRect(),
              taRect: ta.getBoundingClientRect(),
            };
          }
        """)
        btn = info["btnRect"]
        ta = info["taRect"]
        # 按钮中心 vs textarea 中心, 允许 ±3px
        btn_cy = btn["y"] + btn["height"] / 2
        ta_cy = ta["y"] + ta["height"] / 2
        assert abs(btn_cy - ta_cy) <= 3, (
            f"interrupt button must be vertically centered in textarea. "
            f"btn_cy={btn_cy}, ta_cy={ta_cy}, diff={btn_cy - ta_cy}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_interrupt_positioned_right_of_textarea(
    logged_in_page, base_url, test_token
):
    """元素必须在 textarea 右侧 (absolute right). 强制 show 一下来验证布局."""
    sid = api_spawn(base_url, test_token, "/tmp", "interrupt-pos")
    try:
        _enter_chat(logged_in_page, sid)
        info = logged_in_page.evaluate("""
          () => {
            const btn = document.getElementById('chat-interrupt');
            btn.hidden = false;
            const ta = document.getElementById('chat-input');
            const cs = getComputedStyle(btn);
            return {
              position: cs.position,
              btnRect: btn.getBoundingClientRect(),
              taRect: ta.getBoundingClientRect(),
            };
          }
        """)
        assert info["position"] == "absolute", (
            f"chat-interrupt must be absolutely positioned, got {info['position']}"
        )
        btn = info["btnRect"]
        ta = info["taRect"]
        # btn 右边缘应该在 textarea 右边缘以内 (浮在 textarea 内)
        # 允许 8px tolerance (border / padding)
        assert btn["x"] + btn["width"] <= ta["x"] + ta["width"] + 1, (
            f"chat-interrupt should sit inside textarea right side. "
            f"btn_right={btn['x']+btn['width']}, ta_right={ta['x']+ta['width']}"
        )
        assert btn["x"] > ta["x"] + ta["width"] * 0.5, (
            f"chat-interrupt should be on the RIGHT half of textarea. "
            f"btn_x={btn['x']}, ta_mid={ta['x']+ta['width']/2}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
