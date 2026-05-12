"""ClaudeProcess：asyncio.subprocess 封装。stdin/stdout 行级 JSON 双工。

M1 范围：起进程、收事件、发 user message。不接 PreToolUse hook（M3 再加）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
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
        ]
        if self._settings_path:
            cmd += ["--settings", str(self._settings_path)]
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
        cmd = self._build_cmd()
        log.info("spawn claude cwd=%s cmd=%s", self.cwd, " ".join(cmd))

        env = {**os.environ}
        env["CCR_BRIDGE_URL"] = config.BRIDGE_URL
        env["CCR_TOKEN"] = config.TOKEN
        if self.ccr_session_id:
            env["CCR_SESSION_ID"] = self.ccr_session_id

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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

    async def send_user_message(self, text: str) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("ClaudeProcess not started")
        payload = {
            "type": "user",
            "message": {"role": "user", "content": text},
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
        async for raw in self.proc.stdout:
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
