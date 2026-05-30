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
        types.Tool(
            name="share_url",
            description=(
                "Mint a PUBLIC, anonymous HTTPS URL for any file on this "
                "server, served through the CCR hub at "
                "vibe.qpqi.group/files/<short_host>/<id>. Anyone with the "
                "URL can download — no login. The id is 8-byte random and "
                "unguessable, but the URL is a capability — only mint and "
                "share when the user explicitly wants the file to be "
                "publicly fetchable. Different from share_file which renders "
                "an in-chat download card behind CCR auth. Default expires_in_sec "
                "is none (URL never expires); pass a number to auto-expire. "
                "Returns { id, url, expires_at }."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute filesystem path on this server. "
                            "Must exist and be a regular file."
                        ),
                    },
                    "expires_in_sec": {
                        "type": "number",
                        "description": (
                            "Optional. Seconds until URL stops working. "
                            "Omit / 0 / null = never expires."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional short note for your own records.",
                    },
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="share_file",
            description=(
                "Render a prominent file card in the user's CCR chat with a "
                "download button. Use this whenever you have a file the user "
                "should be able to grab as a deliverable — e.g. you generated "
                "a PDF / image / archive / report, downloaded something from "
                "the web for them, or transformed a file. The path must be "
                "absolute and the file must exist on THIS server. Do NOT use "
                "for intermediate / scratch files. Tool returns immediately "
                "with file metadata; the card is rendered from your tool_use "
                "block, not from the result."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute filesystem path on this server. "
                            "Must exist and be a regular file."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "Optional short note shown under the filename "
                            "(e.g. 'PDF version of your docx')."
                        ),
                    },
                },
                "required": ["path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "share_url":
        return await _call_share_url(arguments)
    if name == "share_file":
        return await _call_share_file(arguments)
    if name == "ask_user":
        return await _call_ask_user(arguments)
    return [types.TextContent(type="text", text=f"error: unknown tool {name}")]


async def _call_share_url(arguments: dict) -> list[types.TextContent]:
    """POST /api/share 在 CCR backend 上, 拿 id + 公开 URL 回吐给 claude."""
    path = (arguments.get("path") or "").strip()
    if not path:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": "path required"}, ensure_ascii=False),
        )]
    body = {"path": path}
    if "expires_in_sec" in arguments and arguments["expires_in_sec"]:
        body["expires_in_sec"] = arguments["expires_in_sec"]
    if "note" in arguments and arguments["note"]:
        body["note"] = arguments["note"]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{BACKEND_URL}/api/share",
                headers={"Authorization": f"Bearer {BACKEND_TOKEN}"},
                json=body,
            )
            if r.status_code >= 400:
                return [types.TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"server {r.status_code}: {r.text}"},
                        ensure_ascii=False,
                    ),
                )]
            return [types.TextContent(
                type="text",
                text=json.dumps(r.json(), ensure_ascii=False),
            )]
    except httpx.HTTPError as e:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": f"http: {e}"}, ensure_ascii=False),
        )]


async def _call_share_file(arguments: dict) -> list[types.TextContent]:
    """share_file 不需要 backend POST — 前端从 tool_use input 直接渲卡.
    这里只校验路径 + 返回元数据 (size etc.) 给 claude 看, 让它知道分享成功."""
    path = (arguments.get("path") or "").strip()
    note = arguments.get("note") or ""
    if not path:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": "path required"}, ensure_ascii=False),
        )]
    if not os.path.isabs(path):
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": "path must be absolute"},
                            ensure_ascii=False),
        )]
    if not os.path.isfile(path):
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": f"not a file: {path}"},
                            ensure_ascii=False),
        )]
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": f"stat failed: {e}"},
                            ensure_ascii=False),
        )]
    return [types.TextContent(
        type="text",
        text=json.dumps({
            "ok": True,
            "path": path,
            "name": os.path.basename(path),
            "size": size,
            "note": note,
        }, ensure_ascii=False),
    )]


async def _call_ask_user(arguments: dict) -> list[types.TextContent]:
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
