"""ClaudeCodeRemote 配置：环境变量 + 路径常量。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _require_token() -> str:
    tok = os.environ.get("CCR_TOKEN", "").strip()
    if not tok:
        sys.stderr.write(
            "FATAL: CCR_TOKEN 未设置。请在环境里设一个 bearer token，例如：\n"
            "  CCR_TOKEN=$(openssl rand -hex 16) python3 -m uvicorn server.main:app …\n"
        )
        raise SystemExit(2)
    return tok


def _resolve_claude_bin() -> str:
    explicit = os.environ.get("CCR_CLAUDE_BIN")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    fallback = "/home/hwaipy/.local/nodejs/bin/claude"
    if os.path.exists(fallback):
        return fallback
    sys.stderr.write("FATAL: 找不到 claude CLI；设 CCR_CLAUDE_BIN 指定路径。\n")
    raise SystemExit(2)


TOKEN: str = _require_token()
CLAUDE_BIN: str = _resolve_claude_bin()
HOST: str = os.environ.get("CCR_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("CCR_PORT", "1881"))

DEFAULT_CWD: str = os.environ.get("CCR_DEFAULT_CWD", str(Path.home() / "codes"))

STATIC_DIR: Path = Path(__file__).parent / "static"
