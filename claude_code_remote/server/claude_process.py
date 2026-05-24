"""ClaudeProcess：asyncio.subprocess 封装。stdin/stdout 行级 JSON 双工。

M1 范围：起进程、收事件、发 user message。不接 PreToolUse hook（M3 再加）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)


class ClaudeProcess:
    """单个 claude 子进程的生命周期 + 事件流。

    `events()` 是异步生成器，把 stdout 上每行 JSON 都 yield 出来。stdout 关闭后
    yield 一条 `{"type": "_internal", "subtype": "exit", "returncode": ...}` 然后结束。
    """

    def __init__(self, cwd: str, *, resume_session_id: str | None = None,
                 ccr_session_id: str | None = None,
                 extra_args: list[str] | None = None) -> None:
        self.cwd = cwd
        self.resume_session_id = resume_session_id
        self.ccr_session_id = ccr_session_id
        self.extra_args = list(extra_args or [])
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None  # 由 system/init 事件填充
        self._stderr_buf: list[str] = []
        self._stderr_task: asyncio.Task[None] | None = None
        self._settings_path: Path | None = None
        self._mcp_config_path: Path | None = None

    def _write_mcp_config(self) -> Path:
        """生成临时 mcp-config.json, 注册 CCR 的 ask_user MCP server.

        Claude 启动 MCP server 子进程 (stdio mode), env 通过 config 注入.
        MCP server 拿到 backend URL + token + ccr session id, 触发 ask_user
        tool 时 POST CCR 后端等用户答 — 绕过 SDK builtin AskUserQuestion 的
        硬编码极短 timeout."""
        # http://127.0.0.1:{PORT} 是 CCR server 自己的本地 URL.
        backend_url = f"http://127.0.0.1:{config.PORT}"
        # 用绝对脚本路径而不是 `-m claude_code_remote.mcp.ask_user_server`:
        # claude 启动 MCP server 子进程时 cwd 不是项目根, `-m` 找不到 package
        # (ModuleNotFoundError). 直接跑 .py 文件, python 自动把它父 dir 加进
        # sys.path, 脚本本身只 import 外部 mcp/httpx, 不需要 claude_code_remote.
        mcp_script = str(Path(__file__).resolve().parent.parent
                         / "mcp" / "ask_user_server.py")
        mcp_cfg = {
            "mcpServers": {
                "ccr": {
                    "command": sys.executable,
                    "args": [mcp_script],
                    "env": {
                        "CCR_MCP_BACKEND_URL": backend_url,
                        "CCR_MCP_BACKEND_TOKEN": config.TOKEN,
                        "CCR_MCP_SESSION_ID": self.ccr_session_id or "",
                    },
                },
            },
        }
        fd, p = tempfile.mkstemp(prefix="ccr-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mcp_cfg, f, ensure_ascii=False)
        return Path(p)

    def _write_hook_settings(self) -> Path:
        """生成临时 settings.json，挂 PreToolUse hook 指向桥接器。"""
        settings = {
            "permissions": {"defaultMode": "default"},
            "hooks": {
                "PreToolUse": [{
                    "matcher": ".*",
                    "hooks": [{
                        "type": "command",
                        "command": config.HOOK_BRIDGE,
                    }],
                }],
            },
        }
        fd, p = tempfile.mkstemp(prefix="ccr-settings-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False)
        return Path(p)

    def _build_cmd(self) -> list[str]:
        cmd = [
            config.CLAUDE_BIN,
            "--print",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--include-partial-messages",
            "--include-hook-events",
            "--verbose",
            "--permission-mode", "default",
            # builtin AskUserQuestion 工具 SDK 内 timeout 极短 (实测 < 1s),
            # user 没机会答. 全局禁用, 强制 claude 改用我们的 MCP ask_user.
            "--disallowed-tools", "AskUserQuestion",
        ]
        if self._settings_path:
            cmd += ["--settings", str(self._settings_path)]
        if self._mcp_config_path:
            cmd += ["--mcp-config", str(self._mcp_config_path)]
        if self.resume_session_id:
            cmd += ["--resume", self.resume_session_id]
        cmd += self.extra_args
        return cmd

    async def start(self) -> None:
        if self.proc is not None:
            raise RuntimeError("ClaudeProcess already started")
        if not Path(self.cwd).is_dir():
            raise FileNotFoundError(f"cwd not a directory: {self.cwd}")
        self._settings_path = self._write_hook_settings()
        self._mcp_config_path = self._write_mcp_config()
        cmd = self._build_cmd()
        log.info("spawn claude cwd=%s cmd=%s", self.cwd, " ".join(cmd))

        env = {**os.environ}
        env["CCR_BRIDGE_URL"] = config.BRIDGE_URL
        env["CCR_TOKEN"] = config.TOKEN
        if self.ccr_session_id:
            env["CCR_SESSION_ID"] = self.ccr_session_id

        # limit=16MB：claude stream-json 单行可能很长（图片 base64、大段 tool_result、
        # 长 system message 等）。asyncio 默认 64KB 时 readline 会抛
        # ValueError "Separator is found, but chunk is longer than limit"，
        # pump 任务挂掉。改大避免长行崩 pump。
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=16 * 1024 * 1024,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for line in self.proc.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                log.debug("[claude stderr] %s", text[:300])
                self._stderr_buf.append(text)
                # 保留最近 200 行
                if len(self._stderr_buf) > 200:
                    self._stderr_buf = self._stderr_buf[-200:]

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_buf[-30:])

    async def send_user_message(self, content) -> None:
        """content 可以是 str（纯文本）或 list of block dict（text / image 等）。"""
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("ClaudeProcess not started")
        payload = {
            "type": "user",
            "message": {"role": "user", "content": content},
        }
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        log.debug("send_user_message %d bytes to pid=%s",
                  len(line), self.proc.pid if self.proc else None)
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

    async def close_stdin(self) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except Exception:
                pass

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if not self.proc or not self.proc.stdout:
            raise RuntimeError("ClaudeProcess not started")
        stdout = self.proc.stdout
        while True:
            try:
                raw = await stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as e:
                # 超 buffer 限制 / asyncio 抛 ValueError("Separator is found...")
                # 都不杀 pump：丢这行继续读，下行接得上
                log.warning("stdout readline error (%s); skipping line", e)
                try:
                    # 把剩余的"行"内容吞掉直到换行，下次接力
                    await stdout.readuntil(b"\n")
                except Exception:
                    pass
                yield {"type": "_internal", "subtype": "line_too_long"}
                continue
            if not raw:
                break  # EOF
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                log.warning("non-json stdout: %r", line[:200])
                yield {"type": "_internal", "subtype": "nonjson", "raw": line}
                continue
            # 拦截 system/init 抓 session_id
            if (evt.get("type") == "system" and evt.get("subtype") == "init"
                    and self.session_id is None):
                self.session_id = evt.get("session_id")
            yield evt
        rc = await self.proc.wait()
        yield {"type": "_internal", "subtype": "exit", "returncode": rc}

    async def terminate(self) -> None:
        if not self.proc:
            return
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.proc.kill()
            except ProcessLookupError:
                pass
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._settings_path and self._settings_path.exists():
            with contextlib.suppress(OSError):
                self._settings_path.unlink()
