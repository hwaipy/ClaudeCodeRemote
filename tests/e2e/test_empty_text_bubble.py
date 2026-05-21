"""§4 工具调用后不能留空白 bubble.

stream 协议: assistant message 内 content blocks 顺序可能是 text → tool_use
→ text (空). content_block_start 时乐观 appendBubble("assistant", ""), 准备
接 content_block_delta 写文本. 如果该 text 块没 delta 或 delta 为空,
content_block_stop 时必须删除空 bubble — 否则用户看到 tool-group 下方一个
"不知所云的空白小 card".
"""
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


def test_empty_text_block_does_not_leave_bubble(
    logged_in_page, base_url, test_token
):
    """模拟 stream: message_start → content_block_start(text) → 直接
    content_block_stop (没 delta). content_block_stop 必须清理空 bubble.
    chat-log 内不能出现空 .bubble.assistant."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "empty-bubble")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            // 模拟 stream — message_start + content_block_start(text) +
            // content_block_stop, 中间无 delta
            const fakeMsg = {
              type: 'stream_event',
              event: {
                type: 'message_start',
                message: { id: 'm-empty', model: 'claude', usage: {} },
              },
            };
            handleEvent(fakeMsg.event, Date.now()/1000);
            handleEvent({
              type: 'content_block_start', index: 0,
              content_block: { type: 'text' },
            }, Date.now()/1000);
            // 注: handleStreamEvent 接受 ev.event 直接传 sub-event 字段
            // 实际 handleEvent 会 dispatch type='stream_event' → handleStreamEvent
            // 所以需要包 wrapper.
            handleEvent({
              type: 'stream_event',
              event: {
                type: 'content_block_start', index: 0,
                content_block: { type: 'text' },
              },
            }, Date.now()/1000);
            // 直接 stop (没 delta, text 为空)
            handleEvent({
              type: 'stream_event',
              event: { type: 'content_block_stop', index: 0 },
            }, Date.now()/1000);
            // chat-log 内顶级 .bubble 应该是 0
            const emptyBubbles = Array.from(log.querySelectorAll(
              ':scope > .bubble.assistant'
            )).filter(b => {
              const body = b.querySelector('.msg-body');
              return !body || !body.textContent.trim();
            });
            return {
              emptyCount: emptyBubbles.length,
              totalBubbles: log.querySelectorAll(
                ':scope > .bubble.assistant'
              ).length,
            };
          }
        """)
        assert result["emptyCount"] == 0, (
            f"empty text block must NOT leave a blank bubble: "
            f"got {result['emptyCount']} empty bubble(s) (total: "
            f"{result['totalBubbles']})"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_tool_use_then_empty_text_no_trailing_bubble(
    logged_in_page, base_url, test_token
):
    """常见序: tool_use 块 + tool_use stop + text 块开始 + text stop (空).
    tool-group 应正常显示, 但其后**不能**有空 bubble."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tool-empty-text")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            const now = Date.now()/1000;
            // message_start
            handleEvent({
              type: 'stream_event',
              event: { type: 'message_start',
                message: { id: 'm-tool-text', model: 'claude', usage: {} } },
            }, now);
            // content_block_start tool_use
            handleEvent({
              type: 'stream_event',
              event: { type: 'content_block_start', index: 0,
                content_block: { type: 'tool_use', id: 'tu-1', name: 'Bash' } },
            }, now);
            handleEvent({
              type: 'stream_event',
              event: { type: 'content_block_stop', index: 0 },
            }, now);
            // 然后 text 块, 空
            handleEvent({
              type: 'stream_event',
              event: { type: 'content_block_start', index: 1,
                content_block: { type: 'text' } },
            }, now);
            handleEvent({
              type: 'stream_event',
              event: { type: 'content_block_stop', index: 1 },
            }, now);
            return {
              toolGroups: log.querySelectorAll(':scope > .tool-group').length,
              bubbles: log.querySelectorAll(':scope > .bubble.assistant').length,
            };
          }
        """)
        assert result["toolGroups"] == 1, (
            f"tool_use should yield exactly 1 tool-group: {result}"
        )
        assert result["bubbles"] == 0, (
            f"empty trailing text must NOT leave bubble after tool-group: "
            f"got {result['bubbles']} bubble(s)"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
