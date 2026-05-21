"""§4 Turn-end 通知 (globalWS-driven 版本):

- chat-menu 加 #chat-menu-notify checkbox (label "Notify on turn end")
- 首次勾选 → Notification.requestPermission()
- localStorage.ccr.notifyOnTurnEnd === '1' 持久化
- **任何 session** state transition: prev === "busy" → now !== "busy" 视为 turn end
- + document.hidden + permission granted → new Notification(...)
- tag = sid (同 session 多次替换旧通知)
- 点通知 → window.focus()

监听 globalWS 的 session_state 流, 不依赖用户当前打开的 chat WS — 退回 home
或切到别的 session 也能收到原 session 的 turn-end 通知.
"""
from __future__ import annotations

import pathlib
import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
    page.wait_for_timeout(150)


# ---------- 白盒 ----------

def test_chat_menu_has_notify_checkbox():
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="chat-menu-notify"' in html


def test_app_js_has_notification_flow():
    """白盒: 必须含 requestPermission / new Notification / visibility 检查 /
    localStorage key / busy→idle state-edge 检测."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "Notification.requestPermission" in src
    assert "new Notification(" in src
    assert "visibilityState" in src or "document.hidden" in src
    assert "ccr.notifyOnTurnEnd" in src
    # busy → !busy 转换检测 — 复用现有 _lastNotifiedState Map (toast 已用)
    assert ("_lastNotifiedState" in src
            or "lastStateBySid" in src
            or "prevState" in src), (
        "must track previous state to detect busy→idle transition"
    )


def test_notify_in_globalws_handler():
    """白盒: turn-end 通知 hook 在 globalWS 的 session_state 处理路径上,
    不是 chat 视图内的 result event. 这样退回 home / 切别的 session 也通知.
    实现: session_state handler 调 maybeNotify(msg), 函数内做 busy→!busy
    边沿检测后调 maybeNotifyTurnEnd."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    # 1. session_state handler 段必须调 maybeNotify(
    idx = src.find('type === "session_state"')
    assert idx > 0, "session_state handler not found"
    chunk = src[idx:idx + 800]
    assert "maybeNotify(" in chunk, (
        "globalWS session_state handler must call maybeNotify(msg)"
    )
    # 2. maybeNotify 函数体内必须调 maybeNotifyTurnEnd
    m = re.search(
        r"function maybeNotify\(s\)\s*\{(.*?)^\}", src, re.S | re.M,
    )
    assert m, "function maybeNotify(s) body not found"
    body = m.group(1)
    assert "maybeNotifyTurnEnd" in body, (
        "maybeNotify must call maybeNotifyTurnEnd on busy→!busy edge"
    )


# ---------- 运行时 ----------

def test_notify_fires_on_busy_to_idle_transition(
    logged_in_page, base_url, test_token
):
    """模拟 globalWS 推 session_state: 同一 sid 先 busy 后 idle (turn end).
    notify 必须触发一次, title 含 session name, tag = sid."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "notify-busy-idle")
    try:
        # 留在 home (不进 chat), 验证后台 turn end 也能通知
        result = page.evaluate(f"""
          () => {{
            const calls = [];
            class MockNotification {{
              constructor(title, opts) {{
                calls.push({{ title, opts }});
                this.onclick = null;
              }}
              close() {{}}
              static get permission() {{ return 'granted'; }}
            }}
            window.Notification = MockNotification;
            localStorage.setItem('ccr.notifyOnTurnEnd', '1');
            state.notifyOnTurnEnd = true;
            Object.defineProperty(document, 'visibilityState',
              {{ configurable: true, get: () => 'hidden' }});
            Object.defineProperty(document, 'hidden',
              {{ configurable: true, get: () => true }});

            // 模拟 globalWS 推 busy → idle
            const sess = state.sessionsById.get({sid!r});
            const base = sess ? {{ ...sess }} : {{
              id: {sid!r}, name: 'notify-busy-idle', cwd: '/tmp',
            }};
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'busy' }});
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'idle' }});
            return calls;
          }}
        """)
        assert len(result) == 1, (
            f"expected exactly 1 notification on busy→idle, got {len(result)}: {result}"
        )
        title = result[0].get("title", "")
        assert "notify-busy-idle" in title or "Turn" in title, (
            f"title should mention session name or 'Turn': {title!r}"
        )
        tag = (result[0].get("opts") or {}).get("tag", "")
        assert tag == sid, f"tag should be sid: {tag!r}"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_no_notify_when_visible_and_focused(
    logged_in_page, base_url, test_token
):
    """visible 且 hasFocus → 不弹 (用户真的在看). 任一条件不满足都弹."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "notify-visible-focused")
    try:
        result = page.evaluate(f"""
          () => {{
            const calls = [];
            class MockNotification {{
              constructor(title, opts) {{ calls.push({{ title, opts }}); }}
              static get permission() {{ return 'granted'; }}
            }}
            window.Notification = MockNotification;
            localStorage.setItem('ccr.notifyOnTurnEnd', '1');
            state.notifyOnTurnEnd = true;
            Object.defineProperty(document, 'visibilityState',
              {{ configurable: true, get: () => 'visible' }});
            Object.defineProperty(document, 'hidden',
              {{ configurable: true, get: () => false }});
            document.hasFocus = () => true;

            const sess = state.sessionsById.get({sid!r});
            const base = sess ? {{ ...sess }} : {{
              id: {sid!r}, name: 'x', cwd: '/tmp',
            }};
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'busy' }});
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'idle' }});
            return calls;
          }}
        """)
        assert result == [], f"no notification when visible: {result}"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_notify_fires_when_visible_but_unfocused(
    logged_in_page, base_url, test_token
):
    """visible 但 hasFocus=false (窗口失焦 / 切到别的 app) → 仍弹."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "notify-visible-unfocused")
    try:
        result = page.evaluate(f"""
          () => {{
            const calls = [];
            class MockNotification {{
              constructor(title, opts) {{ calls.push({{ title, opts }}); }}
              static get permission() {{ return 'granted'; }}
            }}
            window.Notification = MockNotification;
            localStorage.setItem('ccr.notifyOnTurnEnd', '1');
            state.notifyOnTurnEnd = true;
            Object.defineProperty(document, 'visibilityState',
              {{ configurable: true, get: () => 'visible' }});
            Object.defineProperty(document, 'hidden',
              {{ configurable: true, get: () => false }});
            document.hasFocus = () => false;   // 窗口失焦

            const sess = state.sessionsById.get({sid!r});
            const base = sess ? {{ ...sess }} : {{
              id: {sid!r}, name: 'notify-visible-unfocused', cwd: '/tmp',
            }};
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'busy' }});
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'idle' }});
            return calls;
          }}
        """)
        assert len(result) == 1, (
            f"visible but unfocused → should still notify, got {result}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_no_notify_when_toggle_off(
    logged_in_page, base_url, test_token
):
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "notify-off-skip")
    try:
        result = page.evaluate(f"""
          () => {{
            const calls = [];
            class MockNotification {{
              constructor(title, opts) {{ calls.push({{ title, opts }}); }}
              static get permission() {{ return 'granted'; }}
            }}
            window.Notification = MockNotification;
            localStorage.removeItem('ccr.notifyOnTurnEnd');
            state.notifyOnTurnEnd = false;
            Object.defineProperty(document, 'visibilityState',
              {{ configurable: true, get: () => 'hidden' }});
            Object.defineProperty(document, 'hidden',
              {{ configurable: true, get: () => true }});

            const sess = state.sessionsById.get({sid!r});
            const base = sess ? {{ ...sess }} : {{
              id: {sid!r}, name: 'x', cwd: '/tmp',
            }};
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'busy' }});
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'idle' }});
            return calls;
          }}
        """)
        assert result == [], result
    finally:
        api_delete_session(base_url, test_token, sid)


def test_only_fires_on_busy_edge(
    logged_in_page, base_url, test_token
):
    """idle → idle (没经过 busy) 不应触发. busy → busy 也不触发."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "notify-edge")
    try:
        result = page.evaluate(f"""
          () => {{
            const calls = [];
            class MockNotification {{
              constructor(title, opts) {{ calls.push({{ title, opts }}); }}
              static get permission() {{ return 'granted'; }}
            }}
            window.Notification = MockNotification;
            localStorage.setItem('ccr.notifyOnTurnEnd', '1');
            state.notifyOnTurnEnd = true;
            Object.defineProperty(document, 'visibilityState',
              {{ configurable: true, get: () => 'hidden' }});
            Object.defineProperty(document, 'hidden',
              {{ configurable: true, get: () => true }});

            const sess = state.sessionsById.get({sid!r});
            const base = sess ? {{ ...sess }} : {{
              id: {sid!r}, name: 'x', cwd: '/tmp',
            }};
            // 一系列非 busy→非 busy 的转换
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'idle' }});
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'idle' }});
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'waiting_permission' }});
            handleGlobalMsg({{ type: 'session_state', ...base, state: 'idle' }});
            return calls;
          }}
        """)
        assert result == [], (
            f"no notification on non-busy edges: {result}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
