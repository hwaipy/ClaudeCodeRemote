"""Cloud Servers 新建 server 时, step2 提供跟 onboarding 同款 Claude Code
prompt — 一键复制扔给新机器上的 Claude Code 自动 setup."""
from __future__ import annotations

import pathlib
import re


def test_module_level_prompt_builder_exists():
    """白盒: 提取出 ccrBuildServerSetupPrompt 让两条路径 (onboarding +
    new-app panel) 共用同一段 prompt 文本, 不复制粘贴漂移."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "function ccrBuildServerSetupPrompt" in src, (
        "应有 module-level ccrBuildServerSetupPrompt 函数"
    )
    # onboarding step2 call
    assert "ccrBuildServerSetupPrompt(host, token, appName)" in src, (
        "onboarding 应通过统一函数生成 prompt"
    )
    # new-app panel call
    assert "ccrBuildServerSetupPrompt(\n" in src \
        or "ccrBuildServerSetupPrompt(location.host" in src, (
        "Cloud Servers 新建 server step2 应通过统一函数生成 prompt"
    )


def test_prompt_contains_essentials():
    """prompt 必须含: 项目名 / wss URL / device token / pip install /
    systemd ExecStart / journalctl 验证 / GitHub 链接 / PyPI 链接."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    # 从 function 体抓
    m = re.search(
        r"function ccrBuildServerSetupPrompt\([^)]*\)\s*\{(.*?)^\}",
        src, re.S | re.M,
    )
    assert m, "ccrBuildServerSetupPrompt body not found"
    body = m.group(1)
    must_have = [
        "ClaudeCodeRemote",
        "${wsUrl}",
        "${token}",
        "${appName}",
        "pip install claude-code-remote",
        "uvicorn claude_code_remote.server.main:app",
        "systemctl --user",
        "hub_client connected",
        "github.com/hwaipy/ClaudeCodeRemote",
        "pypi.org/project/claude-code-remote",
    ]
    for s in must_have:
        assert s in body, f"prompt missing essential bit: {s!r}"


def test_new_app_step2_has_prompt_block():
    """index.html new-app-step2 应含 #new-app-prompt + 复制按钮."""
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="new-app-prompt"' in html
    assert 'id="new-app-prompt-copy"' in html
