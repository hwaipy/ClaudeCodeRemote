"""Model / Effort 在前端三个位置都要能看见 (用户挑哪个留, 先全放):

1. chat-head 的 `#chat-meta` 那行: `~/path · opus · high`
2. ctx-ring tooltip: 加 Model: <值> / Effort: <值> 两行
3. home 卡片: `.card-mef` chip 行

数据来源: server 的 session status_payload 已经回传 model + effort,
前端从 state.sessionsById 拿.
"""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    page.wait_for_timeout(500)


def test_status_payload_includes_model_effort(base_url, test_token):
    """白盒 (server 端): session list / snapshot 必须含 model + effort 字段
    (前端三处显示都靠这个)."""
    from tests.helpers import api_list_sessions, api_delete_session
    sid = api_spawn(base_url, test_token, "/tmp", "status-mef",
                    model="opus", effort="high")
    try:
        rows = api_list_sessions(base_url, test_token)
        match = [r for r in rows if r["id"] == sid]
        assert match, f"spawned session not in list: {sid}"
        s = match[0]
        assert s.get("model") == "opus", f"status_payload model: {s}"
        assert s.get("effort") == "high", f"status_payload effort: {s}"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_meta_shows_model_and_effort(
    logged_in_page, base_url, test_token
):
    """运行时: 进 chat 后 #chat-meta 文本必须包含 model + effort (用 · 分隔)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "chat-meta-mef",
                    model="opus", effort="high")
    try:
        _enter_chat(page, sid)
        # 等 chat-meta 刷新一次 (refreshChatMeta 受 WS 状态 / 首次进入 flow 触发)
        page.wait_for_timeout(300)
        txt = page.locator("#chat-meta").text_content() or ""
        assert "opus" in txt, f"chat-meta should contain model 'opus': {txt!r}"
        assert "high" in txt, f"chat-meta should contain effort 'high': {txt!r}"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_meta_omits_when_default(
    logged_in_page, base_url, test_token
):
    """默认 (空 model + 空 effort) 时, chat-meta 只显示 cwd, 不能拼空字符串."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "chat-meta-default")
    try:
        _enter_chat(page, sid)
        page.wait_for_timeout(300)
        txt = page.locator("#chat-meta").text_content() or ""
        # 不应出现孤立的 · · (说明拼了空段)
        assert "· ·" not in txt, f"chat-meta has empty segments: {txt!r}"
        assert not txt.endswith("·") and not txt.startswith("·"), (
            f"chat-meta has trailing/leading separator: {txt!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_meta_cwd_uses_tilde_for_home(
    logged_in_page, base_url, test_token
):
    """chat-head 的 cwd 必须 abbreviateHome — /home/<u>/X → ~/X. 不能预截到
    "最后两段" (e.g. /home/hwaipy/codes/ccr 不能渲成 codes/ccr 丢 ~)."""
    import os
    page = logged_in_page
    home = os.path.expanduser("~")
    sid = api_spawn(base_url, test_token, home, "chat-meta-tilde")
    try:
        _enter_chat(page, sid)
        page.wait_for_timeout(300)
        txt = page.locator("#chat-meta").text_content() or ""
        assert "~" in txt, (
            f"chat-meta cwd 必须含 ~ (home abbreviated), got: {txt!r}"
        )
        assert "/home/" not in txt and "/Users/" not in txt, (
            f"chat-meta cwd 不应漏出 absolute home prefix: {txt!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_meta_cwd_rtl_truncation_markup():
    """白盒: chat-head 的 cwd 段必须由独立 .meta-cwd 元素承担 (带 RTL 截断).
    JS 端 refreshChatMeta 必须渲染该元素; CSS 端必须有 .meta-cwd 样式规则."""
    import pathlib
    css = pathlib.Path(
        "claude_code_remote/server/static/style.css"
    ).read_text()
    js = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "meta-cwd" in css, (
        ".chat-head .meta-cwd CSS 规则必须存在 (RTL 截断保留右段)"
    )
    assert "meta-cwd" in js, (
        "refreshChatMeta 必须用 .meta-cwd 元素渲染 cwd 段"
    )


# 旧的 ctx-tooltip 交互测试已删 — 功能整合到 #chat-menu, 由
# test_chat_menu.py 覆盖 (open/close, select 改值 → PATCH, ctx 显示).


def test_patch_endpoint_normalizes_bogus(
    logged_in_page, base_url, test_token
):
    """PATCH 端点白盒: 非法 effort 必须 normalize 成空字符串."""
    import httpx
    sid = api_spawn(base_url, test_token, "/tmp", "patch-norm")
    try:
        r = httpx.patch(
            f"{base_url}/api/sessions/{sid}/model_effort",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"model": "opus", "effort": "BOGUS"},
            timeout=5,
        )
        r.raise_for_status()
        body = r.json()
        assert body["model"] == "opus", body
        assert body["effort"] == "", f"bogus effort should normalize: {body}"
    finally:
        api_delete_session(base_url, test_token, sid)
    # NOTE: 上一个 try 块缺 finally, sid 已经 delete. 此函数末尾的 ctx
    # tooltip 测试自身管理状态, 不需要做后置.


def test_home_card_never_shows_mef_chips(
    logged_in_page, base_url, test_token
):
    """home 卡片**永远不**显示 model/effort chip 行. 哪怕 session 有 model,
    session card 也保持两行 (name + cwd/ts), 不加第三行 chip."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "no-mef-chip",
                    model="sonnet", effort="medium")
    try:
        expect(page.locator(f"[data-id='{sid}']")).to_be_visible(timeout=5000)
        card = page.locator(f"[data-id='{sid}']")
        mef = card.locator(".card-mef")
        assert mef.count() == 0, (
            ".card-mef row removed — session card stays at 2 rows even when "
            "session has a non-default model"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
