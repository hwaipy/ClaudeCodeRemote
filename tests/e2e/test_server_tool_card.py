"""server-side 工具卡 (web_search / web_fetch / code_execution) — Anthropic
API 在 stream-json 协议里 emit content_block: server_tool_use → input_json_delta
→ web_search_tool_result. CCR 渲一张 .server-tool-card 显示 query + 结果."""
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


def test_server_tool_card_web_search_streaming(
    logged_in_page, base_url, test_token
):
    """模拟一段 server_tool_use (web_search) + result 流, 验证 .server-tool-card
    渲出来, query 显示, status 从 searching → ✓."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "st-card-1")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.serverToolById = new Map();
            state.blocksByIdx = new Map();
            state.activeMsgId = "msg_test";
            // 1. content_block_start: server_tool_use (web_search)
            handleStreamEvent({
              type: "content_block_start",
              index: 1,
              content_block: {
                type: "server_tool_use",
                id: "srvtoolu_x1",
                name: "web_search",
                input: {},
              },
            });
            // 2. content_block_delta: input streamed
            handleStreamEvent({
              type: "content_block_delta",
              index: 1,
              delta: { type: "input_json_delta",
                       partial_json: '{"query": "latest quantum gate ' },
            });
            handleStreamEvent({
              type: "content_block_delta",
              index: 1,
              delta: { type: "input_json_delta",
                       partial_json: 'fidelity 2025"}' },
            });
            // 3. content_block_stop
            handleStreamEvent({
              type: "content_block_stop", index: 1,
            });
            const pendingCard = log.querySelector('.server-tool-card.pending');
            const pendingQuery = pendingCard?.querySelector('.st-query')?.textContent;

            // 4. result block arrives
            handleStreamEvent({
              type: "content_block_start",
              index: 2,
              content_block: {
                type: "web_search_tool_result",
                tool_use_id: "srvtoolu_x1",
                content: [
                  { type: "web_search_result",
                    url: "https://example.com/a",
                    title: "A: Quantum Gate Fidelity" },
                  { type: "web_search_result",
                    url: "https://example.com/b",
                    title: "B: Surface Code Threshold" },
                ],
              },
            });
            const doneCard = log.querySelector('.server-tool-card.done');
            const links = Array.from(
              doneCard?.querySelectorAll('.st-result') || []
            ).map(a => ({ href: a.href, text: a.textContent }));
            return {
              hadPending: !!pendingCard,
              pendingQuery,
              hasDoneCard: !!doneCard,
              linkCount: links.length,
              firstLinkText: links[0]?.text || "",
              status: doneCard?.querySelector('.st-status')?.textContent || "",
            };
          }
        """)
        assert result["hadPending"], "应先出现 pending server-tool-card"
        assert "quantum gate fidelity" in (result["pendingQuery"] or "").lower(), (
            f"query 应被渲出: {result['pendingQuery']!r}"
        )
        assert result["hasDoneCard"], "结果到了后应变 .done"
        assert result["linkCount"] == 2, (
            f"应渲 2 条结果链接, got {result['linkCount']}"
        )
        assert "Quantum Gate Fidelity" in result["firstLinkText"]
        assert "2" in result["status"], (
            f"status 应显示结果数: {result['status']!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_server_tool_card_backlog_replay(
    logged_in_page, base_url, test_token
):
    """backlog replay (handleAssistantMessage) 也认 server_tool_use / result."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "st-card-replay")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.serverToolById = new Map();
            // backlog 路径: 整条 assistant message 含 server_tool_use + result
            handleAssistantMessage({
              id: "msg_replay",
              content: [
                { type: "text", text: "Let me search:" },
                { type: "server_tool_use",
                  id: "srvtoolu_r1",
                  name: "web_search",
                  input: { query: "PhysRevLett quantum fidelity" } },
                { type: "web_search_tool_result",
                  tool_use_id: "srvtoolu_r1",
                  content: [
                    { type: "web_search_result",
                      url: "https://prl.example.org/x",
                      title: "PRL: Quantum Fidelity" },
                  ] },
              ],
            });
            const card = log.querySelector('.server-tool-card.done');
            return {
              hasCard: !!card,
              query: card?.querySelector('.st-query')?.textContent || "",
              resultCount: card?.querySelectorAll('.st-result').length || 0,
            };
          }
        """)
        assert result["hasCard"], "replay 应渲出 .server-tool-card.done"
        assert "PhysRevLett" in result["query"]
        assert result["resultCount"] == 1
    finally:
        api_delete_session(base_url, test_token, sid)
