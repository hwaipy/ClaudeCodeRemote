"""mcp__ccr__share_file tool 触发独立 file-share 卡 (不是普通 tool-card).
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


def test_share_file_card_rendered_from_tool_use(
    logged_in_page, base_url, test_token
):
    """模拟 mcp__ccr__share_file tool_use block 到达 → 独立 .share-file-card,
    不渲染普通 tool-card."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "sf-card-1")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.toolById = new Map();
            state.shareFileById = new Map();
            state.currentToolGroup = null;
            // 模拟 handleAssistantMessage 收到一条带 share_file tool_use 的 message
            handleAssistantMessage({
              id: 'msg_test',
              content: [
                { type: 'text', text: 'Here you go:' },
                { type: 'tool_use', id: 'tu-sf-1', name: 'mcp__ccr__share_file',
                  input: {
                    path: '/tmp/report.pdf',
                    note: 'Q3 数据报告',
                  } },
              ],
            });
            const sfCard = log.querySelector('.share-file-card');
            const toolCard = log.querySelector('.tool-card');
            return {
              hasSfCard: !!sfCard,
              hasToolCard: !!toolCard,
              fileName: sfCard?.querySelector('.sf-name')?.textContent || "",
              note: sfCard?.querySelector('.sf-note')?.textContent || "",
              dlBtnDisabled: !!sfCard?.querySelector('.sf-download[disabled]'),
            };
          }
        """)
        assert result["hasSfCard"], "应渲染 .share-file-card"
        assert not result["hasToolCard"], (
            "不应同时渲染普通 .tool-card (避免重复)"
        )
        assert result["fileName"] == "report.pdf", (
            f"file name should be basename: {result['fileName']!r}"
        )
        assert "Q3" in result["note"], (
            f"note 字段应显示: {result['note']!r}"
        )
        assert not result["dlBtnDisabled"], (
            "input 已填充, 下载按钮应 enabled"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_share_file_tool_lazy_not_stripped():
    """白盒: strip_payload_for_backlog 不应剥掉 mcp__ccr__share_file 的 input
    — 否则 backlog 回放时 share-file 卡拿不到 path, 下载按钮失效."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/server/tool_lazy.py"
    ).read_text()
    assert "mcp__ccr__share_file" in src, (
        "_is_askuser_tool 应把 mcp__ccr__share_file 也加进不剥列表"
    )


def test_share_file_mcp_tool_registered():
    """白盒: ask_user_server.py 必须 expose share_file tool 给 claude CLI."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/mcp/ask_user_server.py"
    ).read_text()
    assert 'name="share_file"' in src, "MCP server 应注册 share_file tool"
    assert "_call_share_file" in src, "应有 share_file call_tool handler"
