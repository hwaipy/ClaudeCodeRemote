"""§2.2 New session modal: model + effort 选择. spawn 时透传到 claude CLI
(--model / --effort), 持久化到 sessions 表, resume 时同样透传保证整段会话风格一致.

允许:
- model: 空 (= CLI 默认) / "opus" / "sonnet" / "haiku" / 或更详细 model id 字符串
- effort: 空 / "low" / "medium" / "high" / "xhigh" / "max"

非法 effort → 视为空; 非法 model 字符 (非 [A-Za-z0-9._-]) → 视为空 (防 CLI 注入).
"""
from __future__ import annotations

import pathlib
import re
import sqlite3

import httpx
from playwright.sync_api import expect


# ---------- 白盒: 后端 ----------

def test_session_dataclass_has_model_effort():
    src = pathlib.Path(
        "claude_code_remote/server/session_manager.py"
    ).read_text()
    assert re.search(r"^\s*model:\s*str", src, re.M), (
        "Session dataclass must declare model: str"
    )
    assert re.search(r"^\s*effort:\s*str", src, re.M), (
        "Session dataclass must declare effort: str"
    )


def test_spawn_request_has_model_effort():
    src = pathlib.Path(
        "claude_code_remote/server/api.py"
    ).read_text()
    m = re.search(r"class SpawnRequest\(BaseModel\)\s*:\s*\n((?:[ \t].*\n)+)", src)
    assert m, "SpawnRequest not found"
    body = m.group(1)
    assert "model" in body, "SpawnRequest must accept model"
    assert "effort" in body, "SpawnRequest must accept effort"


def test_db_schema_has_model_effort_columns():
    src = pathlib.Path(
        "claude_code_remote/server/db.py"
    ).read_text()
    # CREATE TABLE definition
    assert re.search(r"sessions\s*\([^)]*\bmodel\s+TEXT", src, re.S), (
        "sessions table must declare model TEXT column"
    )
    assert re.search(r"sessions\s*\([^)]*\beffort\s+TEXT", src, re.S), (
        "sessions table must declare effort TEXT column"
    )
    # migration ALTER (for old DBs)
    assert 'ALTER TABLE sessions ADD COLUMN model' in src, (
        "db must have ALTER TABLE migration for model"
    )
    assert 'ALTER TABLE sessions ADD COLUMN effort' in src, (
        "db must have ALTER TABLE migration for effort"
    )
    # _SESS_COLS contains both
    cols_m = re.search(r"_SESS_COLS\s*=\s*\(([^)]+)\)", src)
    assert cols_m, "_SESS_COLS tuple not found"
    cols = cols_m.group(1)
    assert '"model"' in cols and '"effort"' in cols


def test_session_manager_passes_extra_args_to_proc():
    """spawn / resume 都必须把 model/effort 翻译成 --model/--effort 经 extra_args
    传给 ClaudeProcess."""
    src = pathlib.Path(
        "claude_code_remote/server/session_manager.py"
    ).read_text()
    # 翻译辅助函数存在
    assert "_build_cli_extra_args" in src, (
        "_build_cli_extra_args helper must exist"
    )
    # spawn 调用 ClaudeProcess 时带 extra_args
    spawn_m = re.search(
        r"async def spawn\([^)]*\)[^:]*:\s*\n(.*?)\n    async def ",
        src, re.S,
    )
    assert spawn_m, "spawn body not found"
    spawn_body = spawn_m.group(1)
    assert "_build_cli_extra_args" in spawn_body, (
        "spawn must pass _build_cli_extra_args to ClaudeProcess"
    )
    # resume 类似
    resume_m = re.search(
        r"async def resume\([^)]*\)[^:]*:\s*\n(.*?)\n    async def ",
        src, re.S,
    )
    assert resume_m
    resume_body = resume_m.group(1)
    assert "_build_cli_extra_args" in resume_body, (
        "resume must pass _build_cli_extra_args (sess.model, sess.effort)"
    )


def test_norm_model_effort_validators():
    """白盒: 非法 model / effort 必须被规范化为空字符串."""
    import sys
    src_dir = pathlib.Path("claude_code_remote").resolve().parent
    sys.path.insert(0, str(src_dir))
    from claude_code_remote.server import session_manager as sm
    assert sm._norm_effort("HIGH") == "high"   # 大小写
    assert sm._norm_effort("xhigh") == "xhigh"
    assert sm._norm_effort("bogus") == ""
    assert sm._norm_effort("") == ""
    assert sm._norm_effort("  medium  ") == "medium"
    assert sm._norm_model("sonnet") == "sonnet"
    assert sm._norm_model("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert sm._norm_model("bad; rm -rf /") == ""   # 防注入
    assert sm._norm_model("") == ""
    assert sm._norm_model("a" * 80) == ""   # 太长
    assert sm._build_cli_extra_args("sonnet", "high") == [
        "--model", "sonnet", "--effort", "high"
    ]
    assert sm._build_cli_extra_args("", "") == []
    assert sm._build_cli_extra_args("sonnet", "") == ["--model", "sonnet"]


# ---------- 白盒: 前端 ----------

def test_modal_has_model_select_no_effort():
    """前端 spawn modal: model select 已变成 hidden + 空 option (创建时不指定
    model, 由 server wrapper / CLI / chat-menu 决定). effort 完全移除."""
    src = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="spawn-model"' in src, "modal must keep #spawn-model select"
    assert 'id="spawn-effort"' not in src, (
        "spawn-effort select must be removed from modal"
    )
    # spawn-model 现在是 hidden + 单空 option, 不再列 opus/sonnet/haiku
    assert re.search(
        r'<select[^>]*id="spawn-model"[^>]*hidden', src
    ), "spawn-model select 应带 hidden 属性"


def test_spawn_go_posts_model():
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(
        r'\$\("spawn-go"\)\.addEventListener\("click",\s*async[^)]*\)\s*=>\s*\{(.*?)\n\}\);',
        src, re.S,
    )
    assert m, "spawn-go handler not found"
    body = m.group(1)
    assert "$(\"spawn-model\")" in body, "spawn-go must read spawn-model"
    assert "$(\"spawn-effort\")" not in body, (
        "spawn-go must NOT read spawn-effort (UI dropped)"
    )


# ---------- 运行时 ----------

def test_api_spawn_accepts_model_effort(base_url, test_token):
    """POST /api/spawn 接受 model + effort 字段, 200 返回 session id."""
    from tests.helpers import api_spawn, api_delete_session
    sid = api_spawn(base_url, test_token, "/tmp", "model-effort-runtime",
                    model="sonnet", effort="low")
    try:
        assert sid.startswith("ccr-"), f"unexpected sid: {sid}"
    finally:
        api_delete_session(base_url, test_token, sid)


def _spawn_with(base_url, token, model, effort, name):
    """直接 POST 拿原始响应 (不仅是 id), 用于验证 model/effort 回显."""
    r = httpx.post(
        f"{base_url}/api/spawn",
        headers={"Authorization": f"Bearer {token}"},
        json={"cwd": "/tmp", "name": name, "model": model, "effort": effort},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def test_spawn_response_echoes_model_effort(base_url, test_token):
    """spawn API 响应必须回显 server 端 normalize 后的 model + effort
    (前端用来确认实际生效的值, 也用于测试验证)."""
    from tests.helpers import api_delete_session
    body = _spawn_with(base_url, test_token, "opus", "high", "echo-mef")
    try:
        assert body.get("model") == "opus", f"echo model mismatch: {body}"
        assert body.get("effort") == "high", f"echo effort mismatch: {body}"
    finally:
        api_delete_session(base_url, test_token, body["id"])


def test_bogus_effort_normalized_to_empty(base_url, test_token):
    """无效 effort 值不应崩, 响应里 effort 是空字符串 (normalize 兜底)."""
    from tests.helpers import api_delete_session
    body = _spawn_with(base_url, test_token, "", "extreme", "bogus-effort")
    try:
        assert body.get("model") == "" and body.get("effort") == "", (
            f"bogus effort should normalize to '': {body}"
        )
    finally:
        api_delete_session(base_url, test_token, body["id"])


def test_bogus_model_normalized_to_empty(base_url, test_token):
    """注入字符的 model 必须被丢弃 (防 CLI 参数注入)."""
    from tests.helpers import api_delete_session
    body = _spawn_with(base_url, test_token, "bad; rm -rf /", "low",
                       "bogus-model")
    try:
        assert body.get("model") == "", (
            f"injection model must be rejected: {body}"
        )
        assert body.get("effort") == "low", (
            f"effort still valid: {body}"
        )
    finally:
        api_delete_session(base_url, test_token, body["id"])
