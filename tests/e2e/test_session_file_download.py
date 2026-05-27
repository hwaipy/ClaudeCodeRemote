"""GET /api/sessions/<sid>/file?path=<abs> 流式吐文件.
+ tool-card 上的 ⬇ 下载按钮 (input.file_path 存在时显示).
"""
from __future__ import annotations

import os
import re
import tempfile

import httpx
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def test_file_endpoint_streams_existing_file(base_url, test_token):
    """白盒: 给一个真实存在的文件路径, endpoint 返回内容 + Content-Disposition."""
    sid = api_spawn(base_url, test_token, "/tmp", "file-dl-ok")
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        ) as f:
            f.write("hello CCR file download\n")
            p = f.name
        try:
            r = httpx.get(
                f"{base_url}/api/sessions/{sid}/file",
                params={"path": p},
                headers={"Authorization": f"Bearer {test_token}"},
                timeout=5,
            )
            assert r.status_code == 200, r.text
            assert r.content == b"hello CCR file download\n"
            cd = r.headers.get("content-disposition", "")
            assert "attachment" in cd
            assert os.path.basename(p) in cd
        finally:
            os.unlink(p)
    finally:
        api_delete_session(base_url, test_token, sid)


def test_file_endpoint_404_for_missing(base_url, test_token):
    sid = api_spawn(base_url, test_token, "/tmp", "file-dl-404")
    try:
        r = httpx.get(
            f"{base_url}/api/sessions/{sid}/file",
            params={"path": "/tmp/__never_exists__.ccr"},
            headers={"Authorization": f"Bearer {test_token}"},
            timeout=5,
        )
        assert r.status_code == 404
    finally:
        api_delete_session(base_url, test_token, sid)


def test_file_endpoint_400_for_relative(base_url, test_token):
    sid = api_spawn(base_url, test_token, "/tmp", "file-dl-rel")
    try:
        r = httpx.get(
            f"{base_url}/api/sessions/{sid}/file",
            params={"path": "relative/file.txt"},
            headers={"Authorization": f"Bearer {test_token}"},
            timeout=5,
        )
        assert r.status_code == 400
    finally:
        api_delete_session(base_url, test_token, sid)


def test_file_endpoint_400_for_directory(base_url, test_token):
    sid = api_spawn(base_url, test_token, "/tmp", "file-dl-dir")
    try:
        r = httpx.get(
            f"{base_url}/api/sessions/{sid}/file",
            params={"path": "/tmp"},
            headers={"Authorization": f"Bearer {test_token}"},
            timeout=5,
        )
        assert r.status_code == 400
    finally:
        api_delete_session(base_url, test_token, sid)


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
    page.wait_for_timeout(150)


def test_tool_card_shows_download_btn_for_file_path(
    logged_in_page, base_url, test_token
):
    """ensureToolCard + renderToolArgs: 给 input.file_path 的工具卡加 ⬇.
    模拟一个 Write tool 渲染, 检查 .tool-download 出现."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tool-dl-btn")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.currentToolGroup = null;
            state.toolById = new Map();
            // 模拟一个 Write tool
            const entry = ensureToolCard('tool-test-1', 'Write');
            entry.finalInput = {
              file_path: '/tmp/some-output.md',
              content: 'hello',
            };
            renderToolArgs(entry);
            const btn = entry.card.querySelector('.tool-head .tool-download');
            return {
              hasBtn: !!btn,
              btnTitle: btn ? btn.title : "",
            };
          }
        """)
        assert result["hasBtn"], (
            "tool-head 应该有 .tool-download 按钮 (input.file_path 存在)"
        )
        assert "some-output.md" in result["btnTitle"], (
            f"按钮 title 应含 basename: got {result['btnTitle']!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_tool_card_no_download_btn_for_bash(
    logged_in_page, base_url, test_token
):
    """Bash 工具无 file_path → 不加下载按钮."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tool-dl-skip")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.currentToolGroup = null;
            state.toolById = new Map();
            const entry = ensureToolCard('tool-test-2', 'Bash');
            entry.finalInput = { command: 'ls -la' };
            renderToolArgs(entry);
            const btn = entry.card.querySelector('.tool-head .tool-download');
            return { hasBtn: !!btn };
          }
        """)
        assert not result["hasBtn"], (
            "Bash tool (无 file_path) 不应有下载按钮"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
