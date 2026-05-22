"""IndexedDB 浏览器端缓存 + outbox (未 ack user_message 重发):

Step 1: IDB replay 让进 session 立即可见 (0-latency).
Step 2: IDB write 把 server envelope 持久化 (仅可 _idbWriteKind 通过的).
Step 3: outbox — sendUserMessage 写 outbox, server user_input echo 带回
   client_msg_id 时 dequeue; WS reconnect 时 _outboxResend 重发未 ack 的.
"""
from __future__ import annotations

import pathlib
import re
import time

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    card = page.locator(f"[data-id='{sid}']")
    expect(card).to_be_visible(timeout=5000)
    card.click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
    page.wait_for_timeout(300)


def _back_home(page):
    page.locator("#chat-back").dispatch_event("click")
    expect(page.locator("body")).not_to_have_class(
        re.compile(r"\bhas-session\b"), timeout=5000
    )


# ---------- 白盒 ----------

def test_idb_helpers_defined():
    src = pathlib.Path("claude_code_remote/server/static/app.js").read_text()
    for fn in [
        "idbOpen", "idbGetSessionMessages", "idbPutMessage", "idbDeleteSession",
        "idbTrimSession", "idbPutOutbox", "idbDeleteOutbox", "idbListOutboxBySess",
        "_outboxResend", "_idbWriteKind",
    ]:
        assert fn in src, f"missing IDB helper: {fn}"


def test_user_message_carries_client_msg_id():
    """SPA send 必须带 client_msg_id (outbox 配对)."""
    src = pathlib.Path("claude_code_remote/server/static/app.js").read_text()
    # 在 sendUserMessage 函数体内必须有 client_msg_id 字段
    m = re.search(r"function sendUserMessage\(\)\s*\{(.+?)\n\}", src, re.S)
    assert m, "sendUserMessage not found"
    body = m.group(1)
    assert "client_msg_id" in body, (
        "sendUserMessage must include client_msg_id in the frame"
    )
    assert 'type: "user_message"' in body


def test_server_ws_accepts_client_msg_id():
    src = pathlib.Path("claude_code_remote/server/ws.py").read_text()
    assert 'msg.get("client_msg_id")' in src, (
        "server ws.py must read client_msg_id from user_message"
    )
    assert "client_msg_id" in src and '_evt["client_msg_id"]' in src, (
        "server must echo client_msg_id back in user_input event"
    )


def test_idb_write_on_ws_message():
    src = pathlib.Path("claude_code_remote/server/static/app.js").read_text()
    # WS message handler 必须在 seq>0 envelope 上调 idbPutMessage
    assert "idbPutMessage" in src
    # 而且 handler 用 _idbWriteKind 过滤 (跟 server _classify 同款白名单)
    assert "_idbWriteKind(_env.event)" in src or "_idbWriteKind(_env" in src


def test_outbox_dequeue_on_user_input():
    src = pathlib.Path("claude_code_remote/server/static/app.js").read_text()
    # handleUserInput 入口必须查 evt.client_msg_id → idbDeleteOutbox
    m = re.search(r"function handleUserInput\([^)]*\)\s*\{(.{0,400})",
                  src, re.S)
    assert m
    assert "client_msg_id" in m.group(1) and "idbDeleteOutbox" in m.group(1), (
        "handleUserInput must dequeue outbox on echo"
    )


def test_outbox_resend_on_ws_open():
    """connectWS 内 ws.addEventListener('open', ...) 必须调 _outboxResend.
    sendUserMessage 后 ws 断, reconnect 时自动重发未 ack 项."""
    src = pathlib.Path("claude_code_remote/server/static/app.js").read_text()
    # connectWS 是 chat WS, 跟 ws_global 区分. _outboxResend(state.sessionId)
    # 字面值必须出现.
    assert "_outboxResend(state.sessionId)" in src, (
        "connectWS open handler must call _outboxResend(state.sessionId)"
    )


# ---------- 运行时 ----------

def test_idb_caches_envelope_on_first_visit(
    logged_in_page, base_url, test_token,
):
    """进 session → spawn 触发 system_init envelope → IDB messages store 有这条."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "idb-cache-1")
    try:
        _enter_chat(page, sid)
        # 等 backlog_done 落定 + IDB write throttle 跑过
        page.wait_for_timeout(800)
        # 查 IDB
        count = page.evaluate(
            """
            (sid) => new Promise((resolve, reject) => {
              const req = indexedDB.open('ccr', 2);
              req.onsuccess = () => {
                const db = req.result;
                const tx = db.transaction('messages', 'readonly');
                const idx = tx.objectStore('messages').index('by_sess');
                const r = idx.count(IDBKeyRange.only(sid));
                r.onsuccess = () => { db.close(); resolve(r.result); };
                r.onerror = () => reject(r.error);
              };
              req.onerror = () => reject(req.error);
            })
            """, sid)
        assert count >= 1, (
            f"IDB 应有该 session 至少 1 条 envelope (system_init), got {count}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_idb_replay_on_reenter_immediate(
    logged_in_page, base_url, test_token,
):
    """第一次进 session + 发 1 条消息 → IDB 缓存好;
    返回 home → 重新硬刷 (清空 state.sessionCache) → 再进同 session
    → chat-log 应能在 < 1s 渲到该消息 (走 IDB replay 路径)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "idb-replay")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-input").fill("hello idb")
        page.evaluate("sendUserMessage()")
        # 等 fake_claude 完成完整一轮 + IDB write
        page.wait_for_timeout(2000)
        # 硬刷, 清空内存 cache, 只剩 IDB
        page.reload()
        page.wait_for_selector(f"[data-id='{sid}']", timeout=10000)
        # 再次进 — 期望 < 1s 看到 user bubble (从 IDB replay)
        t0 = time.time()
        page.locator(f"[data-id='{sid}']").click()
        page.wait_for_selector(".bubble.user", timeout=2500)
        elapsed = time.time() - t0
        # 给宽松上限 — IDB replay 是异步 transaction, 但应远 < 实际 backlog
        # round trip + render. 2 秒兜底.
        assert elapsed < 2.0, (
            f"IDB replay 期望 <2s, 用了 {elapsed:.2f}s"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_outbox_pending_persisted_to_idb(
    logged_in_page, base_url, test_token,
):
    """点发送但 ws 还没回 echo 那一瞬, outbox 应有该 client_msg_id."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "outbox-pending")
    try:
        _enter_chat(page, sid)
        # 注入测试钩子: hook 当前 WS send 让它不真发, 用来观察 outbox 写入瞬态.
        snapshot = page.evaluate("""
          async () => {
            const realSend = state.ws.send.bind(state.ws);
            state.ws.send = () => {};   // suppress; 不真发, 用来抓 outbox 瞬态
            document.getElementById('chat-input').value = 'hello pending';
            sendUserMessage();
            // 等 IDB write flush
            await new Promise(r => setTimeout(r, 200));
            const result = await new Promise((resolve, reject) => {
              const req = indexedDB.open('ccr', 2);
              req.onsuccess = () => {
                const db = req.result;
                const tx = db.transaction('outbox', 'readonly');
                const idx = tx.objectStore('outbox').index('by_sess');
                const r = idx.getAll(IDBKeyRange.only(state.sessionId));
                r.onsuccess = () => { db.close(); resolve(r.result); };
                r.onerror = () => reject(r.error);
              };
              req.onerror = () => reject(req.error);
            });
            state.ws.send = realSend;
            return result;
          }
        """)
        assert isinstance(snapshot, list) and len(snapshot) >= 1, (
            f"outbox 应有 pending entry: got {snapshot}"
        )
        e = snapshot[0]
        assert e.get("client_msg_id"), f"missing client_msg_id: {e}"
        assert e.get("sess_id") == sid
        assert "hello pending" in (e.get("content") or ""), e
    finally:
        api_delete_session(base_url, test_token, sid)


def test_outbox_dequeued_after_echo(
    logged_in_page, base_url, test_token,
):
    """正常发送路径: server 把 user_input echo 带 client_msg_id 回来 → outbox 清."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "outbox-ack")
    try:
        _enter_chat(page, sid)
        page.wait_for_timeout(300)
        page.locator("#chat-input").fill("hello echo")
        page.evaluate("sendUserMessage()")
        # 等 fake_claude 跑完 + echo 回来
        page.wait_for_timeout(2000)
        remaining = page.evaluate("""
          (sid) => new Promise((resolve, reject) => {
            const req = indexedDB.open('ccr', 2);
            req.onsuccess = () => {
              const db = req.result;
              const tx = db.transaction('outbox', 'readonly');
              const idx = tx.objectStore('outbox').index('by_sess');
              const r = idx.count(IDBKeyRange.only(sid));
              r.onsuccess = () => { db.close(); resolve(r.result); };
              r.onerror = () => reject(r.error);
            };
            req.onerror = () => reject(req.error);
          })
        """, sid)
        assert remaining == 0, (
            f"server echo 后 outbox 应清空: 还剩 {remaining} 条"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
