"""§4 tool-group head 文本支持换行:

合并的工具调用 head 里 .group-count 文本可能很长 (e.g. "Read 5 files,
edited 3 files, running 2 commands"). 之前 CSS 是 flex: 0 0 auto + 默认
white-space, 没换行也没被压缩, 被父 .tool-group 的 overflow: hidden 截掉.

现在 .group-count 必须:
- flex: 1 1 auto + min-width: 0 (允许在 flex 容器内被压缩)
- white-space: normal + word-break/overflow-wrap (允许换行 + 长串断行)
"""
from __future__ import annotations

import pathlib
import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    page.wait_for_timeout(400)


def test_css_group_count_allows_wrap():
    """白盒: .group-count 必须允许 flex 压缩 + 文本换行."""
    css = pathlib.Path(
        "claude_code_remote/server/static/style.css"
    ).read_text()
    m = re.search(r"\.tool-group\s+\.group-count\s*\{([^}]+)\}", css, re.S)
    assert m, ".tool-group .group-count rule not found"
    body = m.group(1)
    # flex 必须允许收缩 (不再是 0 0 auto)
    assert re.search(r"flex\s*:\s*1\s+1\s+auto", body), (
        f".group-count must use flex: 1 1 auto (not 0 0 auto): {body}"
    )
    assert "min-width" in body, (
        f".group-count must declare min-width:0 to actually shrink: {body}"
    )
    # 文本换行规则
    assert re.search(r"white-space\s*:\s*normal", body), (
        f".group-count must use white-space: normal: {body}"
    )
    assert ("word-break" in body) or ("overflow-wrap" in body), (
        f".group-count must enable word-break / overflow-wrap: {body}"
    )


def test_group_count_long_text_wraps_runtime(
    logged_in_page, base_url, test_token
):
    """运行时: 在窄容器里注入超长 group-count 文本, head 必须高于单行
    (= 实际换了行), 文本完整无截断."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "group-head-wrap")
    try:
        _enter_chat(page, sid)
        info = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            // 强制窄一些, 让长文本必换行
            log.style.maxWidth = '420px';

            const group = document.createElement('div');
            group.className = 'tool-group collapsed';
            group.innerHTML = `
              <div class="tool-group-head">
                <span class="group-icon">⚒</span>
                <span class="group-count"></span>
                <span class="group-summary"></span>
                <span class="group-status done"></span>
              </div>
              <div class="tool-group-body"></div>`;
            log.appendChild(group);
            const longText =
              "Read 12 files, edited 7 files, ran 5 commands, "
              + "searched 3 patterns, fetched 2 URLs, updated 4 todos";
            group.querySelector('.group-count').textContent = longText;

            const head = group.querySelector('.tool-group-head');
            const cnt = group.querySelector('.group-count');
            const lineH = parseFloat(getComputedStyle(cnt).lineHeight);
            const headH = head.getBoundingClientRect().height;
            // 截断检测: scrollWidth 应该不超 clientWidth (= 没溢出隐藏)
            return {
              headH, lineH,
              cntScrollW: cnt.scrollWidth,
              cntClientW: cnt.clientWidth,
              displayedText: cnt.textContent,
              expectedText: longText,
            };
          }
        """)
        # 没有水平截断 — scroll 宽度 <= client 宽度
        assert info["cntScrollW"] <= info["cntClientW"] + 1, (
            f".group-count overflows horizontally: "
            f"scrollW={info['cntScrollW']} > clientW={info['cntClientW']} "
            f"— text got clipped by parent overflow:hidden"
        )
        # 已换行 — head 高度明显高于单行 (至少 1.5 倍)
        assert info["headH"] >= info["lineH"] * 1.5, (
            f"head did not wrap: headH={info['headH']:.1f} "
            f"vs lineH={info['lineH']:.1f}"
        )
        # 文本完整
        assert info["displayedText"] == info["expectedText"]
    finally:
        api_delete_session(base_url, test_token, sid)
