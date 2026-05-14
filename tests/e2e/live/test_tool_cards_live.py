"""L4 tool cards in chat — rendered from real backlog.

Picks a session that has tool_use events and that's small enough to render
fast. Verifies the tool group / tool card structure and lazy load.
"""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from tests.pages.home_page import HomePage


@pytest.fixture(scope="module")
def session_with_tools(live_db) -> str:
    """Smallest session that has at least one assistant tool_use event."""
    row = live_db.execute("""
        SELECT s.id, s.name, COUNT(m.seq) AS msg_count
        FROM sessions s
        JOIN messages m ON m.sess_id = s.id
        WHERE s.deleted_at IS NULL
          AND EXISTS (
            SELECT 1 FROM messages m2
            WHERE m2.sess_id = s.id
              AND m2.kind = 'assistant'
              AND m2.payload LIKE '%tool_use%'
          )
        GROUP BY s.id
        HAVING msg_count > 0
        ORDER BY msg_count ASC
        LIMIT 1
    """).fetchone()
    assert row is not None, "no session with tool_use in live fixture"
    return row["id"]


@pytest.fixture(scope="module")
def a_tool_use_id(live_db, session_with_tools) -> str:
    """Pull one concrete tool_use_id out of the chosen session's payloads."""
    for row in live_db.execute(
        "SELECT payload FROM messages WHERE sess_id = ? AND kind = 'assistant'"
        " AND payload LIKE '%tool_use%' LIMIT 50",
        (session_with_tools,),
    ):
        try:
            data = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        msg = data.get("message") or {}
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tuid = block.get("id")
                if tuid:
                    return tuid
    pytest.skip("no tool_use id found in payloads")


def test_tool_group_renders(logged_in_page, session_with_tools):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.card_by_id(session_with_tools).click()

    expect(logged_in_page.locator("#chat-log .tool-group").first
           ).to_be_visible(timeout=10000)


def test_tool_card_has_name_text(logged_in_page, session_with_tools):
    """Each tool group has a head showing the tool name; one of Read/Bash/Edit/
    Write/Glob/Grep/WebFetch/Task/TodoWrite/MCP is likely to appear."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.card_by_id(session_with_tools).click()

    first_group = logged_in_page.locator("#chat-log .tool-group").first
    expect(first_group).to_be_visible(timeout=10000)
    # head text contains some non-empty string
    head_text = first_group.locator(".tool-group-head").first.inner_text()
    assert head_text.strip(), "tool group head was empty"


def test_lazy_tool_payload_endpoint(live_server_env, session_with_tools,
                                    a_tool_use_id):
    """Spec: clicking a lazy tool card hits GET /api/sessions/<sid>/tool/<tuid>.
    Hit the endpoint directly to make sure it works against live data."""
    import httpx
    r = httpx.get(
        f"{live_server_env['base_url']}/api/sessions/"
        f"{session_with_tools}/tool/{a_tool_use_id}",
        headers={"Authorization": f"Bearer {live_server_env['token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "name" in body or "has_result" in body
