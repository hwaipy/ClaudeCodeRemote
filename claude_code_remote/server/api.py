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
    permission_mode: str = "manual"
    model: str = ""    # claude CLI --model (alias 或完整 id); 空 = 用 CLI 默认
    effort: str = ""   # claude CLI --effort: low/medium/high/xhigh/max; 空 = 默认


class DbgLogRequest(BaseModel):
    tag: str
    data: Any = None


@router.get("/cli/defaults")
async def cli_defaults() -> dict[str, Any]:
    """读 ~/.claude/settings.json 解析用户配置的 model / effort 默认值.
    前端 chat-menu 的 Default 选项文本注释用 — claude stream event 只报
    cur_model, 不报 effort, settings.json 是探知 effort 默认的唯一线索.
    文件不存在 / 解析失败 / 字段缺失 → 对应字段返回 null."""
    import json
    out: dict[str, Any] = {"model": None, "effort": None}
    path = Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            m = data.get("model")
            e = data.get("effort")
            if isinstance(m, str) and m:
                out["model"] = m
            if isinstance(e, str) and e:
                out["effort"] = e
    except Exception:
        pass
    return out


class ModelEffortRequest(BaseModel):
    # Optional 区分: None = 不改这字段, 空字符串 = 显式清掉.
    # 前端 effort UI 已移除, 默认不发 effort 字段 → 不动 sess.effort.
    model: str | None = None
    effort: str | None = None


@router.patch("/sessions/{session_id}/model_effort")
async def update_session_model_effort(
    session_id: str, req: ModelEffortRequest,
) -> dict[str, Any]:
    """改 session 的 model / effort. None 字段保持原值, 字符串字段写入."""
    ok = await manager.update_model_effort(session_id, req.model, req.effort)
    if not ok:
        raise HTTPException(404, "session not found")
    sess = await manager.get(session_id)
    return {"model": sess.model, "effort": sess.effort}


@router.post("/dbg/log")
async def dbg_log(req: DbgLogRequest) -> dict[str, Any]:
    """前端调试钩子：把任意标签+数据打到服务端 INFO 日志，远端可在 journalctl 里查。"""
    log.info("DBG %s %s", req.tag, req.data)
    return {"ok": True}


@router.post("/spawn")
async def spawn(req: SpawnRequest) -> dict[str, Any]:
    cwd = os.path.expanduser(req.cwd)
    if not Path(cwd).is_dir():
        raise HTTPException(400, f"cwd not a directory: {cwd}")
    sess = await manager.spawn(cwd=cwd, name=req.name,
                                model=req.model, effort=req.effort)
    if req.permission_mode and req.permission_mode != "manual":
        try:
            await gateway.set_mode(sess.id, req.permission_mode)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return {
        "id": sess.id,
        "cwd": sess.cwd,
        "name": sess.name,
        "created_at": sess.created_at,
        "model": sess.model,
        "effort": sess.effort,
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


class MkdirRequest(BaseModel):
    parent: str
    name: str = Field(..., min_length=1)


@router.post("/mkdir")
async def mkdir(req: MkdirRequest) -> dict[str, Any]:
    """在指定父目录下创建子目录（目录浏览器"新建文件夹"用）。"""
    parent_raw = (req.parent or "").strip()
    parent = Path(os.path.expanduser(parent_raw)).resolve()
    if not parent.is_dir():
        raise HTTPException(400, f"parent is not a directory: {parent}")
    # 文件夹名安全：禁 / \ 以及前后空白；只在目标父目录下创建一级
    name = req.name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(400, "invalid folder name")
    target = parent / name
    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        raise HTTPException(409, f"already exists: {target}")
    except PermissionError:
        raise HTTPException(403, f"permission denied: {parent}")
    except OSError as e:
        raise HTTPException(400, f"mkdir failed: {e}")
    return {"path": str(target)}


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
    from .tool_lazy import strip_payload_for_backlog
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    limit = max(1, min(100, limit))
    rows = await db.load_messages_before(session_id, before_seq, limit)
    messages = [{"seq": s, "ts": t, "event": strip_payload_for_backlog(p)}
                for s, t, _k, p in rows]
    first_seq = messages[0]["seq"] if messages else None
    has_more = (await db.count_messages_before(session_id, first_seq) > 0) if first_seq is not None else False
    return {"messages": messages, "first_seq": first_seq, "has_more": has_more}


def _read_last_assistant_usage(jsonl: Path) -> dict[str, Any] | None:
    """从 ~/.claude/projects/<cwd>/<sid>.jsonl 末尾扒最新一条 assistant message 的 usage。
    用于进 session 时立即显示 ctx，而不用等下次 message_start。"""
    import json as _json
    try:
        sz = jsonl.stat().st_size
    except FileNotFoundError:
        return None
    read_size = min(sz, 100 * 1024)   # 最后 100 KB 应该足以找到最近一条 assistant
    with jsonl.open("rb") as f:
        f.seek(max(0, sz - read_size))
        data = f.read().decode("utf-8", errors="ignore")
    for raw in reversed(data.split("\n")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = _json.loads(raw)
        except _json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        if d.get("isSidechain"):
            continue   # subagent 的 usage 不是主对话的
        msg = d.get("message") or {}
        u = msg.get("usage") or {}
        if not u:
            continue
        return {
            "model": msg.get("model", ""),
            "input_tokens": u.get("input_tokens", 0),
            "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
        }
    return None


@router.get("/sessions/{session_id}/ctx")
async def session_ctx(session_id: str) -> dict[str, Any]:
    """读 Claude CLI 的 session jsonl 拿当前 context 用量（不用等下次模型调用）。"""
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    if not sess.claude_session_id:
        return {"available": False, "reason": "no claude session id yet"}
    encoded = sess.cwd.replace("/", "-")
    p = Path.home() / ".claude" / "projects" / encoded / f"{sess.claude_session_id}.jsonl"
    loop = asyncio.get_event_loop()
    usage = await loop.run_in_executor(None, _read_last_assistant_usage, p)
    if not usage:
        return {"available": False, "reason": "no usage found"}
    return {"available": True, **usage}


@router.get("/sessions/{session_id}/file")
async def session_file(session_id: str, path: str = "") -> Any:
    """下载 session 工作目录下的文件 — 给前端 tool-card 上的下载按钮用.

    安全模型: token-protected (require_token), 路径必须绝对存在且
    是普通文件. 不限制文件位置 — server 跑哪儿就能拿哪儿 (跟 claude
    CLI Read/Write 工具一样的边界). 不解析 symlink target, 但 resolve
    会 follow, 这点跟 ls/mkdir 一致.
    """
    from fastapi.responses import FileResponse
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    raw = (path or "").strip()
    if not raw:
        raise HTTPException(400, "path required")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise HTTPException(400, "absolute path required")
    p = Path(expanded).resolve()
    if not p.exists():
        raise HTTPException(404, f"file not found: {p}")
    if not p.is_file():
        raise HTTPException(400, f"not a file: {p}")
    # 用 FileResponse 的 filename= 参数, starlette 内部按 RFC 5987 处理
    # 非 ASCII 文件名 (filename* 字段). 手动 Content-Disposition 走 latin-1
    # 会爆中文 — 别覆盖.
    return FileResponse(
        str(p),
        filename=p.name,
        media_type="application/octet-stream",   # 强制 attachment 行为
        content_disposition_type="attachment",
    )


@router.get("/sessions/{session_id}/tool/{tool_use_id}")
async def get_tool_payload(session_id: str, tool_use_id: str) -> dict[str, Any]:
    """前端展开折叠卡时按需拉工具调用内容。
    优先从内存 live_tools 取（含运行中的 partial_input）；找不到再回退到 DB。"""
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    live = sess.live_tools.get(tool_use_id) if sess.live_tools else None
    if live is not None:
        return {
            "name": live.get("name", ""),
            "input": live.get("input"),
            "partial_input": live.get("partial_input") or "",
            "result": live.get("result"),
            "is_error": bool(live.get("is_error")),
            "has_result": bool(live.get("completed")),
            "completed": bool(live.get("completed")),
        }
    from .tool_lazy import find_tool_payload
    data = await find_tool_payload(session_id, tool_use_id)
    if data["input"] is None and not data["has_result"]:
        raise HTTPException(404, "tool not found")
    return {**data, "partial_input": "", "completed": True}


class PermissionModeRequest(BaseModel):
    mode: str


@router.get("/sessions/{session_id}/permission_mode")
async def get_permission_mode(session_id: str) -> dict[str, Any]:
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    return {"mode": gateway.get_mode(session_id)}


@router.put("/sessions/{session_id}/permission_mode")
async def set_permission_mode(session_id: str, req: PermissionModeRequest) -> dict[str, Any]:
    sess = await manager.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    try:
        await gateway.set_mode(session_id, req.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.info("permission mode set: sess=%s mode=%s", session_id, req.mode)
    # 切到 allow_all / accept_edits / plan → 把已经在等待的请求按新规则处理
    resolved = 0
    if req.mode == "allow_all":
        for preq in gateway.pending_for_session(session_id):
            ok = await gateway.resolve(preq.req_id,
                                       {"behavior": "allow",
                                        "updatedInput": preq.tool_input,
                                        "message": "allow_all mode"})
            if ok:
                resolved += 1
                await manager.inject_event(sess, {
                    "type": "_ccr",
                    "subtype": "permission_resolved",
                    "req_id": preq.req_id,
                    "decision": "allow",
                    "message": "allow_all mode",
                })
    elif req.mode == "accept_edits":
        from .permission_gateway import _EDIT_TOOLS
        for preq in gateway.pending_for_session(session_id):
            if preq.tool_name not in _EDIT_TOOLS:
                continue
            ok = await gateway.resolve(preq.req_id,
                                       {"behavior": "allow",
                                        "updatedInput": preq.tool_input,
                                        "message": "accept_edits mode"})
            if ok:
                resolved += 1
                await manager.inject_event(sess, {
                    "type": "_ccr",
                    "subtype": "permission_resolved",
                    "req_id": preq.req_id,
                    "decision": "allow",
                    "message": "accept_edits mode",
                })
    elif req.mode == "plan":
        for preq in gateway.pending_for_session(session_id):
            ok = await gateway.resolve(preq.req_id,
                                       {"behavior": "deny",
                                        "message": "plan mode"})
            if ok:
                resolved += 1
                await manager.inject_event(sess, {
                    "type": "_ccr",
                    "subtype": "permission_resolved",
                    "req_id": preq.req_id,
                    "decision": "deny",
                    "message": "plan mode",
                })
    # 广播 mode 变更到 WS（其它窗口同步 UI）
    await manager.inject_event(sess, {
        "type": "_ccr",
        "subtype": "permission_mode",
        "mode": req.mode,
    })
    return {"mode": req.mode, "auto_resolved": resolved}


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


@router.put("/sessions/{session_id}/rename")
async def rename_session(session_id: str, req: RenameRequest) -> dict[str, Any]:
    """Rename a session (user-facing name)."""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    ok = await manager.rename(session_id, name)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True, "name": name}


@router.post("/sessions/{session_id}/seen")
async def mark_session_seen(session_id: str) -> dict[str, Any]:
    """用户进 chat 看了这个 session → 标记已读 (清未读蓝点). seen_at 存
    server 端, 广播 session_state 让所有设备同步. idempotent."""
    ok = await manager.mark_seen(session_id)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True}


@router.post("/sessions/{session_id}/activate")
async def activate_session(session_id: str) -> dict[str, Any]:
    """Move a previously-deactivated session back into the Active bucket."""
    ok = await manager.activate(session_id)
    if not ok:
        # Either session not found, or it wasn't deactivated. Idempotent OK.
        from .session_manager import manager as _m
        sess = await _m.get(session_id)
        if not sess:
            raise HTTPException(404, "session not found")
    return {"ok": True}


@router.post("/sessions/{session_id}/deactivate")
async def deactivate_session(session_id: str) -> dict[str, Any]:
    """Move session into the 'Inactive' bucket; no process change."""
    ok = await manager.deactivate(session_id)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True}


@router.post("/sessions/{session_id}/stash")
async def stash_session(session_id: str) -> dict[str, Any]:
    """Move session into the 'Stash' bucket (above Inactive, default
    expanded). No process change. Mutually exclusive with Inactive."""
    ok = await manager.stash(session_id)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True}


@router.post("/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str) -> dict[str, Any]:
    """打断当前会话：杀掉子进程。下一次发消息或显式 resume 会自动重启。"""
    ok = await manager.interrupt(session_id)
    if not ok:
        raise HTTPException(404, "session not running")
    return {"ok": True}


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


async def _handle_askuser_hook(sess: Any, tool_use_id: str,
                                tool_input: dict[str, Any]) -> dict[str, Any]:
    """AskUserQuestion 的 PreToolUse hook：把整条 hook 调用挂着等用户在前端答完，
    然后 **在 return allow 之前** 把 tool_result 写到 claude stdin（管道缓冲里）。

    ⚠️ 当前只是半成品。已验证：前端能渲卡片、用户能交互、答案能写回 DB。
       但 claude CLI 在 `--print` 模式下 emit tool_use 之后会**自己内部**合成一条
       `is_error=true content="Answer questions?"` 的 tool_result 加到它发往
       Anthropic API 的消息流里——我们 stdin 注入的真答案在它消息流里是第二条，
       Anthropic 端通常会用第一条，agent 最终回的还是 "Answer questions?"。
       这是 CLI 设计层的限制，靠 hook 无法绕过。
       真正可用要走 MCP 自定义工具或代理 Anthropic API（见项目 follow-up）。"""
    import json
    aq = await gateway.open_askuser(sess.id, tool_use_id, tool_input)
    log.info("askuser hook opened: req=%s sess=%s tool_use_id=%s",
             aq.req_id, sess.id, tool_use_id)
    await manager.inject_event(sess, {
        "type": "_ccr",
        "subtype": "askuser_request",
        "req_id": aq.req_id,
        "tool_use_id": tool_use_id,
        "tool_input": tool_input,
    })
    try:
        answer = await asyncio.wait_for(aq.future,
                                         timeout=PERMISSION_WAIT_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("askuser timeout: req=%s", aq.req_id)
        await gateway.resolve_askuser_by_tool_id(tool_use_id, None)
        await manager.inject_event(sess, {
            "type": "_ccr",
            "subtype": "askuser_resolved",
            "req_id": aq.req_id,
            "tool_use_id": tool_use_id,
            "cancelled": True,
        })
        return {"behavior": "deny", "message": "askuser timeout"}
    if answer is None:
        # session 取消或没答案
        await manager.inject_event(sess, {
            "type": "_ccr",
            "subtype": "askuser_resolved",
            "req_id": aq.req_id,
            "tool_use_id": tool_use_id,
            "cancelled": True,
        })
        return {"behavior": "deny", "message": "askuser cancelled"}
    # 用户答完：先给 claude 持久化一条 user/tool_result（供前端 backlog 回放看到答案），
    # 再 stdin 灬一条 tool_result 给 CLI 读，最后才 return allow。
    content_text = json.dumps(answer, ensure_ascii=False)
    blocks = [{
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content_text,
    }]
    await manager.inject_event(sess, {
        "type": "user",
        "message": {"role": "user", "content": blocks},
    })
    try:
        await sess.proc.send_user_message(blocks)
    except Exception:
        log.exception("askuser stdin write failed sess=%s", sess.id)
        return {"behavior": "deny", "message": "stdin write failed"}
    log.info("askuser hook resolving allow: req=%s sess=%s", aq.req_id, sess.id)
    return {"behavior": "allow", "updatedInput": tool_input,
            "message": "askuser answered"}


class McpAskUserRequest(BaseModel):
    ccr_session_id: str
    tool_use_id: str
    questions: list[dict[str, Any]]


@router.post("/mcp/ask_user")
async def mcp_ask_user(req: McpAskUserRequest) -> dict[str, Any]:
    """MCP custom ask_user tool 后端. 跟 _handle_askuser_hook 平行, 但
    不写 stdin (MCP tool return value IS the tool_result, claude 通过 MCP
    协议直接读到). 复用 gateway + SPA WS, 体验完全一致."""
    import json as _json
    sess = await manager.get(req.ccr_session_id)
    if not sess:
        raise HTTPException(404, "ccr session not found")
    aq = await gateway.open_askuser(
        sess.id, req.tool_use_id, {"questions": req.questions},
    )
    log.info("mcp ask_user opened: req=%s sess=%s tool_use_id=%s",
             aq.req_id, sess.id, req.tool_use_id)
    await manager.inject_event(sess, {
        "type": "_ccr",
        "subtype": "askuser_request",
        "req_id": aq.req_id,
        "tool_use_id": req.tool_use_id,
        "tool_input": {"questions": req.questions},
    })
    try:
        answer = await asyncio.wait_for(aq.future,
                                         timeout=PERMISSION_WAIT_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("mcp ask_user timeout: req=%s", aq.req_id)
        await gateway.resolve_askuser_by_tool_id(req.tool_use_id, None)
        await manager.inject_event(sess, {
            "type": "_ccr", "subtype": "askuser_resolved",
            "req_id": aq.req_id, "tool_use_id": req.tool_use_id,
            "cancelled": True,
        })
        return {"answers": None, "error": "timeout"}
    if answer is None:
        await manager.inject_event(sess, {
            "type": "_ccr", "subtype": "askuser_resolved",
            "req_id": aq.req_id, "tool_use_id": req.tool_use_id,
            "cancelled": True,
        })
        return {"answers": None, "error": "cancelled"}
    # persist visible tool_result event so SPA history shows the answer too
    # (matches hook path UX); MCP path skips stdin write entirely.
    await manager.inject_event(sess, {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": req.tool_use_id,
                "content": _json.dumps(answer, ensure_ascii=False),
            }],
        },
    })
    log.info("mcp ask_user resolved: req=%s sess=%s", aq.req_id, sess.id)
    return {"answers": answer}


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

    # AskUserQuestion 走独立的 hook-hold 流程：用户没回答前我们就把整条 hook 调用挂着；
    # 等用户回答后再给 stdin 灬 tool_result，并 return allow。这样 CLI 不会触发 auto-fail。
    if tool_name == "AskUserQuestion":
        return await _handle_askuser_hook(sess, tool_use_id, tool_input)

    # plan mode: deny everything before it ever reaches the user
    if gateway.is_plan_mode(sid):
        return {"behavior": "deny", "message": "plan mode (no tool execution)"}

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
