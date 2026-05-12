"""HTTP API。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_token
from .permission_gateway import gateway
from .session_manager import manager

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


class SpawnRequest(BaseModel):
    cwd: str = Field(..., min_length=1)
    name: str = ""


@router.post("/spawn")
async def spawn(req: SpawnRequest) -> dict[str, Any]:
    cwd = os.path.expanduser(req.cwd)
    if not Path(cwd).is_dir():
        raise HTTPException(400, f"cwd not a directory: {cwd}")
    sess = await manager.spawn(cwd=cwd, name=req.name)
    return {
        "id": sess.id,
        "cwd": sess.cwd,
        "name": sess.name,
        "created_at": sess.created_at,
    }


@router.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    return {"sessions": await manager.list_sessions()}


@router.get("/sessions/{session_id}/stderr")
async def stderr_tail(session_id: str) -> dict[str, Any]:
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    return {"stderr": sess.proc.stderr_tail}


class PermissionWaitRequest(BaseModel):
    ccr_session_id: str
    claude_payload: dict[str, Any]


PERMISSION_WAIT_TIMEOUT_S = 580.0  # 略短于桥接器 600s


@router.post("/permission/wait")
async def permission_wait(req: PermissionWaitRequest) -> dict[str, Any]:
    """PreToolUse hook 桥接器 POST 进来。命中白名单直接 allow，否则推 WS 等用户决定。"""
    sid = req.ccr_session_id
    sess = await manager.get(sid)
    if not sess:
        raise HTTPException(404, "ccr session not found")
    payload = req.claude_payload or {}
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    tool_use_id = payload.get("tool_use_id", "")

    if gateway.is_preapproved(sid, tool_name, tool_input):
        log.info("permission preapproved: sess=%s tool=%s", sid, tool_name)
        return {"behavior": "allow", "updatedInput": tool_input,
                "message": "preapproved"}

    preq = await gateway.open_request(ccr_session_id=sid, claude_payload=payload)
    log.info("permission request opened: req=%s sess=%s tool=%s",
             preq.req_id, sid, tool_name)
    await manager.inject_event(sess, {
        "type": "_ccr",
        "subtype": "permission_request",
        "req_id": preq.req_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
    })
    try:
        decision = await asyncio.wait_for(preq.future,
                                           timeout=PERMISSION_WAIT_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("permission timeout: req=%s", preq.req_id)
        await gateway.resolve(preq.req_id,
                              {"behavior": "deny", "message": "timeout"})
        decision = {"behavior": "deny", "message": "timeout"}

    # 把决定也广播一份到 WS（让 UI 把卡片打钩 / 关掉）
    await manager.inject_event(sess, {
        "type": "_ccr",
        "subtype": "permission_resolved",
        "req_id": preq.req_id,
        "decision": decision.get("behavior"),
        "message": decision.get("message", ""),
    })
    return decision
