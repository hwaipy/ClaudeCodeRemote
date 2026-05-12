"""HTTP API。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_token
from .session_manager import manager

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
