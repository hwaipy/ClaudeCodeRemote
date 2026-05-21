"""§4: chat-head 是 position:fixed, chat-log 的视觉 box 必须整个落在
chat-head 下面 — 否则:

1) chat-log 的滚动条沿右边缘从 y=0 起跑, top 段被 chat-head 盖一半;
2) #history-loader 用 chat-log.getBoundingClientRect().top + 8 定位,
   chat-log.top 若 = 0, loader 就钉在 y=8 (chat-head 后);
3) 消息内容 (clientRect 不包含 padding) 被 head 遮.

之前的做法是给 chat-log 加 padding-top, 但 padding 只推**内容**, 不影响
scrollbar 起点和绝对定位子元素 — 所以现在把 head clearance 挪到 .chat
容器上 (padding-top), chat-log 本身从 head bottom 开始.
"""
from __future__ import annotations

import re
import pathlib

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    page.wait_for_timeout(500)


def test_css_chat_container_reserves_head_height():
    """白盒: 头高度的让位写在 .chat 容器的 padding-top 上, 用
    var(--chat-head-h, ...). 不能写在 .chat-log 上 (padding 不挡滚动条
    起点, 也不挡 absolute/fixed 子元素的定位锚)."""
    css = pathlib.Path(
        "claude_code_remote/server/static/style.css"
    ).read_text()
    # .chat 块内必须含 padding-top with --chat-head-h
    m = re.search(r"^\.chat\s*\{([^}]*)\}", css, re.S | re.M)
    assert m, ".chat rule not found"
    chat_body = m.group(1)
    assert "--chat-head-h" in chat_body and "padding-top" in chat_body, (
        f".chat must declare padding-top using var(--chat-head-h); "
        f"got: {chat_body}"
    )


def test_chat_log_does_not_reserve_head_padding():
    """白盒: chat-log 自己不再加 head-h 的 padding-top — 那是父 .chat
    的事. chat-log 仍可以有零或常数 padding-top (跟 head 高度无关)."""
    css = pathlib.Path(
        "claude_code_remote/server/static/style.css"
    ).read_text()
    m = re.search(r"\.chat-log\s*\{([^}]*)\}", css, re.S)
    assert m, ".chat-log rule not found"
    body = m.group(1)
    # padding-top 不应当再用 --chat-head-h
    pad_top = re.search(r"padding-top\s*:\s*([^;]+);", body)
    if pad_top:
        assert "--chat-head-h" not in pad_top.group(1), (
            f".chat-log must NOT bake --chat-head-h into its own padding-top; "
            f"the clearance lives on .chat. got: {pad_top.group(1)}"
        )


def test_js_writes_chat_head_h_from_resize_observer():
    """白盒: 必须有 syncChatHeadHeight() + ResizeObserver 观察 chat-head.
    隐藏 (display:none → bbox h=0) 时必须把 --chat-head-h 写 0, 否则
    head 不见了但 .chat 还在留头高度的空白."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "syncChatHeadHeight" in src
    assert "--chat-head-h" in src
    m = re.search(
        r"new ResizeObserver\(syncChatHeadHeight\)\.observe\(\s*head", src
    )
    assert m, "syncChatHeadHeight must be wired to a ResizeObserver on chat-head"
    # h==0 (head display:none) 必须显式写 0, 不能保留旧值
    fn = re.search(
        r"function syncChatHeadHeight\(\)\s*\{(.*?)^\}", src, re.S | re.M
    )
    assert fn, "syncChatHeadHeight body not found"
    body = fn.group(1)
    assert re.search(r"--chat-head-h.*0", body, re.S), (
        "syncChatHeadHeight must explicitly set --chat-head-h to 0px when "
        "the head is hidden / has zero height"
    )


def test_chat_log_top_at_or_below_head_bottom(
    logged_in_page, base_url, test_token
):
    """运行时: chat-log 的视觉 box top ≥ chat-head bottom (允许 1 px 误差).
    这样滚动条整段都在 head 下方, top 段不会被 head 盖.

    用 chat-log getBoundingClientRect 的 .top, 不是 padding-top — 我们
    要 box 本身就在 head 下面, 不是靠 padding 推内容."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "log-below-head")
    try:
        _enter_chat(page, sid)
        info = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            const head = document.getElementById('chat-head');
            const lr = log.getBoundingClientRect();
            const hr = head.getBoundingClientRect();
            return { logTop: lr.top, headBottom: hr.bottom, headH: hr.height };
          }
        """)
        assert info["logTop"] >= info["headBottom"] - 1, (
            f"chat-log top ({info['logTop']}) must be ≥ chat-head bottom "
            f"({info['headBottom']}); else scrollbar top is hidden under head"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_history_loader_visible_below_head(
    logged_in_page, base_url, test_token
):
    """运行时: 上拉加载时显示的 history-loader card, 它的整张 card 必须
    位于 chat-head 之下 (top ≥ head bottom - 1px). 之前的 bug: loader
    用 chat-log.top + 8 定位, 而 chat-log.top = 0 (跟 head 重叠), loader
    就藏在 head 后看不见."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "loader-visible")
    try:
        _enter_chat(page, sid)
        # 显式调 setHistoryLoader 让它出现; 不依赖触发条件
        info = page.evaluate("""
          () => {
            setHistoryLoader("Loading earlier messages…");
            const el = document.getElementById('history-loader');
            const head = document.getElementById('chat-head');
            const er = el.getBoundingClientRect();
            const hr = head.getBoundingClientRect();
            return {
              loaderTop: er.top, loaderH: er.height,
              headBottom: hr.bottom, hidden: el.hidden,
            };
          }
        """)
        assert not info["hidden"], "history-loader should be visible after call"
        assert info["loaderTop"] >= info["headBottom"] - 1, (
            f"history-loader top ({info['loaderTop']}) must be ≥ chat-head "
            f"bottom ({info['headBottom']}); else loader is hidden behind head"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
