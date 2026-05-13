"""按需加载工具调用内容：backlog/分页时剔除大字段，前端展开时再 HTTP 拉。

`tool_use.input` 和 `tool_result.content` 在历史回放里通常很大但用户多数情况不会去看。
推 WS 前用 strip_payload_for_backlog 改写成 `{"__ccr_lazy": True, ...}` 占位；
点开折叠卡时前端调 `/api/sessions/{sid}/tool/{tool_use_id}` 拉真身。
DB 始终存全量原文，本模块只做读路径上的瘦身。"""
from __future__ import annotations

from typing import Any

from . import db

CCR_LAZY = "__ccr_lazy"
CCR_SUMMARY = "__ccr_summary"
CCR_SIZE = "__ccr_size"


def _summarize_tool_input(name: str, input_obj: Any) -> str:
    """跟前端 toolSummary() 对齐：折叠卡头需要的一行预览。"""
    if not isinstance(input_obj, dict):
        return ""
    if name == "Bash":
        cmd = input_obj.get("command") or ""
        return cmd.split("\n", 1)[0] if isinstance(cmd, str) else ""
    if name in ("Read", "Write", "Edit"):
        p = input_obj.get("file_path") or ""
        if isinstance(p, str):
            return "/".join(p.split("/")[-2:])
        return ""
    if name in ("Glob", "Grep"):
        return input_obj.get("pattern") or ""
    if name == "WebFetch":
        return input_obj.get("url") or ""
    if name == "WebSearch":
        return input_obj.get("query") or ""
    if name == "TodoWrite":
        todos = input_obj.get("todos")
        if isinstance(todos, list):
            n = len(todos)
            if not n:
                return ""
            return f"{n} item" if n == 1 else f"{n} items"
    return ""


def _result_size(content: Any) -> int:
    """大概估一下结果文本量，前端可显示 "(X chars)"。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for x in content:
            if isinstance(x, dict) and isinstance(x.get("text"), str):
                n += len(x["text"])
        return n
    return 0


def strip_payload_for_backlog(payload: dict[str, Any]) -> dict[str, Any]:
    """浅拷贝并改写：tool_use.input / tool_result.content → 懒占位。
    其它字段不动；原 payload 不被改写。"""
    t = payload.get("type")
    if t == "assistant":
        msg = payload.get("message") or {}
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            return payload
        changed = False
        new_blocks: list[Any] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = b.get("name", "")
                summary = _summarize_tool_input(name, b.get("input"))
                nb = {**b, "input": {CCR_LAZY: True, CCR_SUMMARY: summary}}
                new_blocks.append(nb)
                changed = True
            else:
                new_blocks.append(b)
        if changed:
            return {**payload, "message": {**msg, "content": new_blocks}}
        return payload

    if t == "user":
        msg = payload.get("message") or {}
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            return payload
        changed = False
        new_blocks = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                size = _result_size(b.get("content"))
                nb = {**b, "content": {CCR_LAZY: True, CCR_SIZE: size}}
                new_blocks.append(nb)
                changed = True
            else:
                new_blocks.append(b)
        if changed:
            return {**payload, "message": {**msg, "content": new_blocks}}
        return payload

    return payload


def update_live_tools(sess: Any, evt: dict[str, Any]) -> None:
    """从 raw 事件流里抽取工具调用的实时状态，写到 sess.live_tools / sess.block_to_tool。
    跟 strip_live_event 同时调用：strip 决定下不下发，update 保证内存里有完整状态供 /tool/{id} 拉。"""
    t = evt.get("type")
    if t == "stream_event":
        ev = evt.get("event") or {}
        et = ev.get("type")
        if et == "message_start":
            sess.block_to_tool.clear()
        elif et == "content_block_start":
            idx = ev.get("index")
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                tid = cb.get("id")
                if tid is not None:
                    sess.block_to_tool[idx] = tid
                    init_input = cb.get("input")
                    sess.live_tools[tid] = {
                        "name": cb.get("name", ""),
                        "partial_input": "",
                        "input": init_input if isinstance(init_input, dict) and init_input else None,
                        "result": None,
                        "is_error": False,
                        "completed": False,
                    }
        elif et == "content_block_delta":
            idx = ev.get("index")
            d = ev.get("delta") or {}
            if d.get("type") == "input_json_delta":
                tid = sess.block_to_tool.get(idx)
                if tid and tid in sess.live_tools:
                    sess.live_tools[tid]["partial_input"] += d.get("partial_json") or ""
    elif t == "assistant":
        msg = evt.get("message") or {}
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = b.get("id")
                if tid is None:
                    continue
                cur = sess.live_tools.get(tid)
                if cur is None:
                    cur = {
                        "name": b.get("name", ""),
                        "partial_input": "",
                        "input": None,
                        "result": None,
                        "is_error": False,
                        "completed": False,
                    }
                    sess.live_tools[tid] = cur
                # 最终 input 以 assistant message 为准
                if isinstance(b.get("input"), dict):
                    cur["input"] = b.get("input")
                if b.get("name"):
                    cur["name"] = b.get("name")
    elif t == "user":
        msg = evt.get("message") or {}
        blocks = msg.get("content") or []
        if not isinstance(blocks, list):
            return
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid is None:
                    continue
                cur = sess.live_tools.get(tid)
                if cur is None:
                    cur = {
                        "name": "",
                        "partial_input": "",
                        "input": None,
                        "result": None,
                        "is_error": False,
                        "completed": False,
                    }
                    sess.live_tools[tid] = cur
                cur["result"] = b.get("content")
                cur["is_error"] = bool(b.get("is_error"))
                cur["completed"] = True


def strip_live_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """实时 WS 广播路径上的瘦身。返回 None = 此事件不下发；否则返回（可能瘦身的）新 payload。
    DB 不走这条路径，原始 payload 仍然全量入库，按需走 /tool/{id} 拉回。"""
    t = payload.get("type")
    if t == "assistant" or t == "user":
        return strip_payload_for_backlog(payload)
    if t == "stream_event":
        ev = payload.get("event") or {}
        et = ev.get("type")
        if et == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                # 把 input 替换为占位；保留 id/name/type 让前端能起卡片
                new_cb = {**cb, "input": {CCR_LAZY: True, CCR_SUMMARY: ""}}
                return {**payload, "event": {**ev, "content_block": new_cb}}
        elif et == "content_block_delta":
            d = ev.get("delta") or {}
            # 工具参数的分块 stream 全部不下发；最终 assistant 事件里给 summary 就够了
            if d.get("type") == "input_json_delta":
                return None
    return payload


async def find_tool_payload(sess_id: str, tool_use_id: str) -> dict[str, Any]:
    """扫 DB 找指定 tool_use_id 的完整 input + result。
    简单实现：load 全部消息后线性扫；若性能成问题再加专门索引。"""
    rows = await db.load_messages(sess_id)
    out: dict[str, Any] = {
        "name": "",
        "input": None,
        "result": None,
        "is_error": False,
        "has_result": False,
    }
    for _seq, _ts, _kind, payload in rows:
        t = payload.get("type")
        if t == "assistant":
            msg = payload.get("message") or {}
            for b in msg.get("content") or []:
                if (isinstance(b, dict)
                        and b.get("type") == "tool_use"
                        and b.get("id") == tool_use_id):
                    out["input"] = b.get("input")
                    out["name"] = b.get("name", "")
        elif t == "user":
            msg = payload.get("message") or {}
            blocks = msg.get("content") or []
            if isinstance(blocks, list):
                for b in blocks:
                    if (isinstance(b, dict)
                            and b.get("type") == "tool_result"
                            and b.get("tool_use_id") == tool_use_id):
                        out["result"] = b.get("content")
                        out["is_error"] = bool(b.get("is_error"))
                        out["has_result"] = True
    return out
