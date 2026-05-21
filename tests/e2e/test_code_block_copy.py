"""§4 assistant bubble 代码块一键复制:

markdown ` ```lang ` 渲染成 <pre><code>. 每个 <pre> 必须包到 .code-block-wrap,
右上角加一个 .copy-code-btn (📋 Copy). 点击复制 <code>.textContent 到剪贴板,
按钮变 "Copied" 1.5s.

兼容流式 (renderMDIntoBubble 多次写 innerHTML): 每次扫描幂等, 不重复包.
"""
from __future__ import annotations

import pathlib
import re

from playwright.sync_api import expect


def test_renderMD_adds_copy_button_to_pre():
    """白盒: app.js 必须有 wrap 包裹 <pre> + 加 .copy-code-btn 的逻辑."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "code-block-wrap" in src, (
        "missing .code-block-wrap (the <pre> wrapper)"
    )
    assert "copy-code-btn" in src, (
        "missing .copy-code-btn (the floating copy button)"
    )
    # 必须用 navigator.clipboard.writeText
    assert "clipboard" in src and "writeText" in src, (
        "missing clipboard.writeText call for copy"
    )


def test_code_block_runtime(logged_in_page):
    """运行时: 注入一个 assistant bubble 含 ```bash 代码块, 渲染后:
       - <pre> 被 .code-block-wrap 包住
       - 出现 .copy-code-btn
       - 点击按钮: textContent 被复制到 clipboard
       - 按钮短暂变 "Copied"
    """
    page = logged_in_page
    # 给 clipboard 权限
    ctx = page.context
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:
        pass
    page.evaluate("""
      () => {
        const log = document.getElementById('chat-log');
        log.innerHTML = '';
        const bubble = document.createElement('div');
        bubble.className = 'bubble assistant';
        bubble.innerHTML = '<div class="msg-body"></div>';
        log.appendChild(bubble);
        renderMDIntoBubble(bubble,
          'before\\n\\n```bash\\necho hello world\\nls -la\\n```\\n\\nafter');
      }
    """)
    page.wait_for_timeout(80)
    # 结构验证
    wrap_count = page.evaluate(
        "() => document.querySelectorAll('.bubble .code-block-wrap').length"
    )
    assert wrap_count == 1, f"expected 1 .code-block-wrap, got {wrap_count}"
    btn_count = page.evaluate(
        "() => document.querySelectorAll('.bubble .copy-code-btn').length"
    )
    assert btn_count == 1, f"expected 1 .copy-code-btn, got {btn_count}"
    # 再次 render 同 text — 幂等不应重复包
    page.evaluate("""
      () => {
        const bubble = document.querySelector('.bubble.assistant');
        renderMDIntoBubble(bubble,
          'before\\n\\n```bash\\necho hello world\\nls -la\\n```\\n\\nafter');
      }
    """)
    page.wait_for_timeout(50)
    wrap_count2 = page.evaluate(
        "() => document.querySelectorAll('.bubble .code-block-wrap').length"
    )
    assert wrap_count2 == 1, (
        f"renderMD should be idempotent; got {wrap_count2} wraps after 2nd render"
    )
    # 按钮是 icon-only (SVG), 没文字; 状态切换用 .copied class
    btn = page.locator(".copy-code-btn")
    assert btn.locator("svg").count() == 1, (
        "copy-code-btn must contain a single <svg> icon"
    )
    # 按钮常显 (不靠 hover) — 验证 opacity 接近 1
    opacity_before_hover = page.evaluate(
        "() => parseFloat(getComputedStyle("
        "document.querySelector('.copy-code-btn')).opacity)"
    )
    assert opacity_before_hover >= 0.9, (
        f"copy-code-btn must be always-visible (opacity ≥ 0.9), "
        f"got {opacity_before_hover}"
    )
    # pre 必须有 padding-right 留位防文字盖按钮
    pad_right = page.evaluate(
        "() => parseFloat(getComputedStyle("
        "document.querySelector('.code-block-wrap > pre')).paddingRight)"
    )
    assert pad_right >= 30, (
        f"pre padding-right must reserve space for the button, got {pad_right}"
    )
    # 点击切 .copied class
    btn.click()
    page.wait_for_timeout(80)
    assert "copied" in (page.locator(".copy-code-btn").get_attribute("class") or ""), (
        "button must add .copied class after click"
    )
    # 1.5s 后回弹
    page.wait_for_timeout(1700)
    cls_after = page.locator(".copy-code-btn").get_attribute("class") or ""
    assert "copied" not in cls_after, (
        f".copied class must be removed after 1.5s, got class={cls_after!r}"
    )
