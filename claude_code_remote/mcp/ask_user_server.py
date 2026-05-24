"""MCP stdio server — 暴露 ask_user tool 给 Claude CLI.

绕过 builtin AskUserQuestion 工具的硬编码极短 timeout (实测 < 1s, user 在
PWA 上根本来不及答). MCP tool call 由我们控时序, claude SDK 不知道里面在
等什么, 不会自己 timeout. 长 await 直到 user 答完 (或 580s 兜底超时).

启动方式: claude --mcp-config <临时 json>, json 里指 command/env, claude
spawn 这个 server 子进程, stdio 协议跟 claude 直通信. server 通过 env
拿 CCR backend URL / token / ccr_session_id:

    CCR_MCP_BACKEND_URL=http://127.0.0.1:1884
    CCR_MCP_BACKEND_TOKEN=<server token>
    CCR_MCP_SESSION_ID=<ccr-xxxxxx>

实现: ask_user tool 收到 questions → POST CCR /api/mcp/ask_user
(无 timeout) → CCR 推 WS 给 SPA → user 答 → CCR resolve → 我们 return
JSON 文本作为 tool_result, claude 直接读到. 全程 SDK 无插手.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

BACKEND_URL = os.environ.get("CCR_MCP_BACKEND_URL", "http://127.0.0.1:1881").rstrip("/")
BACKEND_TOKEN = os.environ.get("CCR_MCP_BACKEND_TOKEN", "")
SESSION_ID = os.environ.get("CCR_MCP_SESSION_ID", "")

app: Server = Server("ccr")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="ask_user",
            description=(
                "Ask the human user one or more multiple-choice questions "
                "and wait for their answer. This is the ONLY way to ask the "
                "user a structured question in this environment — the builtin "
                "AskUserQuestion tool has a broken timeout and must not be "
                "used. This call blocks until the user answers (or 580s)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "header": {"type": "string",
                                           "description": "short label (≤12 chars)"},
                                "multiSelect": {"type": "boolean", "default": False},
                                "options": {
                                    "type": "array",
                                    "minItems": 2,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string"},
                                            "description": {"type": "string"},
                                        },
                                        "required": ["label", "description"],
                                    },
                                },
                            },
                            "required": ["question", "header", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "ask_user":
        return [types.TextContent(type="text", text=f"error: unknown tool {name}")]
    if not SESSION_ID:
        return [types.TextContent(type="text",
                                  text="error: CCR_MCP_SESSION_ID not configured")]
    tool_use_id = "mcp-" + uuid.uuid4().hex[:12]
    questions = arguments.get("questions") or []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as c:
            r = await c.post(
                f"{BACKEND_URL}/api/mcp/ask_user",
                headers={"Authorization": f"Bearer {BACKEND_TOKEN}"},
                json={
                    "ccr_session_id": SESSION_ID,
                    "tool_use_id": tool_use_id,
                    "questions": questions,
                },
            )
            r.raise_for_status()
            body = r.json()
            return [types.TextContent(
                type="text",
                text=json.dumps(body, ensure_ascii=False),
            )]
    except httpx.HTTPError as e:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": f"{type(e).__name__}: {e!s}"},
                            ensure_ascii=False),
        )]


async def _main() -> None:
    async with stdio_server() as (rs, ws):
        await app.run(rs, ws, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
