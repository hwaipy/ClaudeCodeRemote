"""HTTP API。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import re
import time
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
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


class LsResponse(BaseModel):
    path: str
    parent: str | None
    dirs: list[str]


@router.get("/ls")
async def ls(path: str = "") -> LsResponse:
    """目录浏览：返回某个绝对路径下的子目录列表 + 父目录。点目录浏览器用。"""
    from .config import DEFAULT_CWD
    raw = (path or "").strip() or DEFAULT_CWD
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise HTTPException(400, "请使用绝对路径（以 / 开头），或 ~ 开头")
    p = Path(expanded).resolve()
    if not p.exists():
        raise HTTPException(400, f"路径不存在：{p}")
    if not p.is_dir():
        raise HTTPException(400, f"不是目录：{p}")
    try:
        dirs = sorted(
            (e.name for e in os.scandir(p)
             if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
            key=str.lower,
        )
    except PermissionError:
        raise HTTPException(403, f"无权限读取：{p}")
    parent = None if p == p.parent else str(p.parent)
    return LsResponse(path=str(p), parent=parent, dirs=dirs)


@router.get("/sessions/{session_id}/stderr")
async def stderr_tail(session_id: str) -> dict[str, Any]:
    sess = await manager.get(session_id)
    if not sess or sess.proc is None:
        raise HTTPException(404, "session not found / not running")
    return {"stderr": sess.proc.stderr_tail}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, before_seq: int, limit: int = 20) -> dict[str, Any]:
    """向前翻页拉历史：返回 seq < before_seq 的最近 limit 条（按 seq 升序）。"""
    from . import db
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    limit = max(1, min(100, limit))
    rows = await db.load_messages_before(session_id, before_seq, limit)
    messages = [{"seq": s, "ts": t, "event": p} for s, t, _k, p in rows]
    first_seq = messages[0]["seq"] if messages else None
    has_more = (await db.count_messages_before(session_id, first_seq) > 0) if first_seq is not None else False
    return {"messages": messages, "first_seq": first_seq, "has_more": has_more}


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: str) -> dict[str, Any]:
    sess = await manager.resume(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    return {
        "id": sess.id,
        "claude_session_id": sess.claude_session_id,
        "cwd": sess.cwd,
        "name": sess.name,
    }


_SAFE_NAME = re.compile(r"[^\w.\-]+", re.UNICODE)
_MAX_UPLOAD = 50 * 1024 * 1024   # 50 MB 上限，防止误传超大文件打爆磁盘


@router.post("/sessions/{session_id}/upload")
async def upload_to_session(session_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    raw_name = (file.filename or "upload").split("/")[-1].split("\\")[-1]
    safe = _SAFE_NAME.sub("_", raw_name).strip("._") or "upload"
    target_dir = Path(sess.cwd) / ".ccr-uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{int(time.time())}-{safe}"
    size = 0
    with target.open("wb") as fp:
        while True:
            chunk = await file.read(1 << 20)   # 1 MB 片段
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_UPLOAD:
                fp.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, f"file too large (> {_MAX_UPLOAD} bytes)")
            fp.write(chunk)
    log.info("upload: sess=%s file=%s size=%d → %s", session_id, raw_name, size, target)
    return {"path": str(target), "name": raw_name, "size": size}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    ok = await manager.delete(session_id)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True}


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
