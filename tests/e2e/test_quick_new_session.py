"""§2 无感新建 session (Quick new):

- 点 #new-btn-quick (icon 按钮, 跟 #new-btn 同款外形) → 立即进空白 chat,
  不弹 modal, 不起 claude CLI 子进程
- 前端临时 sid = "tmp-<hex>", state.sessionsById 加 _pending: true
- 没发消息离开 → tmp session 从 state.sessionsById 删, db 完全没记录
- 首条 send → POST /api/spawn 拿真 sid, 替换 tmp; rename 用消息前 30 字符

NOTE: #new-btn-quick 按钮当前 **暂时隐藏** (HTML 加了 hidden 属性),
整个 quick-new 流程的代码 (handler / _spawnFromTmpAndSend / tmp session lifecycle)
保留待恢复, 但 UI 入口禁用 — 整个 test 文件 skip. 想恢复: 去掉 HTML 上的
hidden + 去掉这里的 skip 标记.
"""
from __future__ import annotations

import re
import pathlib

import httpx
import pytest
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_list_sessions

pytestmark = pytest.mark.skip(
    reason="#new-btn-quick 暂时隐藏, 代码保留待恢复 (见模块 docstring)"
)


# ---------- 白盒 ----------

def test_new_btn_quick_exists_in_home_top():
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="new-btn-quick"' in html, "missing #new-btn-quick button"
    # 必须在 home-top 内 (跟 #new-btn 同处)
    idx = html.find('class="home-top"')
    end = html.find("</div>", idx + 50)
    chunk = html[idx:end + 600]
    assert 'id="new-btn-quick"' in chunk, (
        "#new-btn-quick must be inside .home-top"
    )


def test_app_js_handles_tmp_session_prefix():
    """白盒: 必须含 tmp- 前缀检查和 _pending 处理路径."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "tmp-" in src, "tmp- prefix mechanism missing"
    assert "_pending" in src, "_pending session flag missing"
    # quick new btn handler
    assert 'new-btn-quick' in src, "missing #new-btn-quick handler"


# ---------- 运行时 ----------

def test_quick_new_creates_blank_session_card(
    logged_in_page, base_url, test_token
):
    """点 #new-btn-quick: 立即进 chat, session list 多一张空白卡,
    后端 db 没新 session."""
    page = logged_in_page
    before = api_list_sessions(base_url, test_token)
    before_ids = {s["id"] for s in before}

    page.locator("#new-btn-quick").click()
    page.wait_for_timeout(200)
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=5000
    )
    # 当前 sessionId 必须是 tmp-
    sid = page.evaluate("() => state.sessionId")
    assert sid and sid.startswith("tmp-"), f"sessionId should be tmp-: {sid!r}"
    # state.sessionsById 内有这个 sid 且 _pending=true
    pending = page.evaluate(f"""
      () => {{
        const s = state.sessionsById.get({sid!r});
        return s && s._pending === true;
      }}
    """)
    assert pending, "tmp session must have _pending: true"
    # 后端 db 没新 session
    after = api_list_sessions(base_url, test_token)
    after_ids = {s["id"] for s in after}
    new_ids = after_ids - before_ids
    assert not new_ids, (
        f"backend should have NO new session yet, got: {new_ids}"
    )


def test_leaving_without_sending_deletes_tmp(
    logged_in_page, base_url, test_token
):
    """点 quick → 进空 chat → 不发消息 → 离开 (模拟 back) → tmp 消失."""
    page = logged_in_page
    page.locator("#new-btn-quick").click()
    page.wait_for_timeout(200)
    sid = page.evaluate("() => state.sessionId")
    assert sid and sid.startswith("tmp-")
    # 模拟 chat-back: 调 cleanup + enterHome (宽屏下 #chat-back 视觉 hidden,
    # 但 click handler 行为我们直接 dispatch). 现实里宽屏没 back 按钮,
    # 用户切别的 session 或刷新页面同样应清掉 tmp.
    page.evaluate("() => document.getElementById('chat-back').click()")
    page.wait_for_timeout(200)
    exists = page.evaluate(f"() => state.sessionsById.has({sid!r})")
    assert not exists, f"tmp session must vanish after back: {sid}"
    card_count = page.locator(f'[data-id="{sid}"]').count()
    assert card_count == 0, f"tmp card should be gone, count={card_count}"


def test_first_send_spawns_real_session(
    logged_in_page, base_url, test_token
):
    """tmp session 内发消息: 真 spawn, state.sessionId 切到真 sid,
    db 多一条 session, 名字 = 消息前 30 字符."""
    page = logged_in_page
    before = api_list_sessions(base_url, test_token)
    before_ids = {s["id"] for s in before}

    page.locator("#new-btn-quick").click()
    page.wait_for_timeout(200)
    tmp_sid = page.evaluate("() => state.sessionId")
    assert tmp_sid.startswith("tmp-")

    # 在 chat input 输入并发送
    page.locator("#chat-input").fill("debug the parser issue 12345")
    page.locator("#chat-input").press("Enter")
    # 等 spawn + WS connect + first_paint (fake_claude 起步 ~1s)
    page.wait_for_timeout(3000)

    # 当前 sessionId 切到真 ccr- id
    real_sid = page.evaluate("() => state.sessionId")
    assert real_sid and real_sid.startswith("ccr-"), (
        f"sessionId should be real ccr- after send: {real_sid!r}"
    )

    # db 多一条 session, name = 消息前 30 字符
    after = api_list_sessions(base_url, test_token)
    new = [s for s in after if s["id"] not in before_ids]
    assert len(new) == 1, f"exactly 1 new session in db: {[s['id'] for s in new]}"
    name = new[0]["name"]
    assert "debug the parser issue" in name, (
        f"session name should derive from message text: {name!r}"
    )

    api_delete_session(base_url, test_token, new[0]["id"])
