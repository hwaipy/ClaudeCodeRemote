"""未读蓝点: 跑过且结束、还没被看过的 session 标记. seen_at 服务端维护,
跨设备 (任何设备点开都算看过)."""
from __future__ import annotations

import re
import time

import httpx
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _sessions(base_url, token):
    r = httpx.get(f"{base_url}/api/sessions",
                  headers={"Authorization": f"Bearer {token}"}, timeout=5)
    r.raise_for_status()
    return {s["id"]: s for s in r.json()["sessions"]}


# ---------- server 端 ----------

def test_fresh_session_not_unseen(base_url, test_token):
    """新建 session 还没跑 → seen_at == created_at == last_activity_at →
    不是未读 (last_activity_at 不 > seen_at)."""
    sid = api_spawn(base_url, test_token, "/tmp", "fresh")
    try:
        s = _sessions(base_url, test_token)[sid]
        assert s["seen_at"] >= s["last_activity_at"], (
            f"新 session seen_at 应 >= last_activity_at (基线), "
            f"got seen_at={s['seen_at']} la={s['last_activity_at']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_seen_endpoint_bumps_seen_at(base_url, test_token):
    """POST /seen → seen_at = max(now, last_activity_at) ≥ last_activity_at."""
    sid = api_spawn(base_url, test_token, "/tmp", "mark-seen")
    try:
        # 人为把 last_activity_at 推到未来 (模拟跑过一轮), 然后标记已读
        before = _sessions(base_url, test_token)[sid]
        r = httpx.post(f"{base_url}/api/sessions/{sid}/seen",
                       headers={"Authorization": f"Bearer {test_token}"},
                       timeout=5)
        assert r.status_code == 200, r.text
        after = _sessions(base_url, test_token)[sid]
        assert after["seen_at"] >= after["last_activity_at"], (
            f"标记已读后 seen_at 应 >= last_activity_at: {after}"
        )
        assert after["seen_at"] >= before["seen_at"]
    finally:
        api_delete_session(base_url, test_token, sid)


def test_seen_endpoint_404_missing(base_url, test_token):
    r = httpx.post(f"{base_url}/api/sessions/ccr-nope/seen",
                   headers={"Authorization": f"Bearer {test_token}"}, timeout=5)
    assert r.status_code == 404


# ---------- 前端 unseen 判据 ----------

def _enter_home(page):
    expect(page.locator("#view-home")).to_be_visible(timeout=10000)
    page.wait_for_timeout(150)


def test_frontend_unseen_dot_logic(logged_in_page, base_url, test_token):
    """前端: idle + last_activity_at > seen_at → 卡片 .unseen + state-dot 变蓝;
    busy / 已读 / 没跑过 → 不变蓝.

    蓝色复用同一颗 .state-dot 元素 (idle/finished 默认是空心/灰, 加 .unseen
    时覆盖为蓝实心), 不增加新的 UI 元素."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "dot-logic")
    try:
        _enter_home(page)
        page.wait_for_timeout(300)
        result = page.evaluate(f"""
          () => {{
            const sid = "{sid}";
            const base = (state.sessionsById.get(sid)) || {{
              id: sid, name: 'x', cwd: '/tmp', created_at: 1000,
            }};
            // 蓝色定义: state-dot background 是 #2f81f7 (47,129,247)
            const isBlue = (rgb) => /\\b47,\\s*129,\\s*247\\b/.test(rgb);
            const mk = (patch) => {{
              state.sessionsById.set(sid, Object.assign({{}}, base, patch));
              renderSessionList();
              const card = document.querySelector(
                `.session-card[data-id='${{sid}}']`);
              const dot = card && card.querySelector('.state-dot');
              const bg = dot ? getComputedStyle(dot).backgroundColor : '';
              return {{
                hasUnseenClass: !!card && card.classList.contains('unseen'),
                dotIsBlue: isBlue(bg),
                // 应该只有一颗 state-dot, 不该有独立的 .unseen-dot 元素
                separateDotExists: !!card &&
                  !!card.querySelector('.unseen-dot'),
                bg,
              }};
            }};
            return {{
              idleUnseen: mk({{ state: 'idle',
                last_activity_at: 2000, seen_at: 1000 }}),
              idleSeen: mk({{ state: 'idle',
                last_activity_at: 2000, seen_at: 2000 }}),
              busy: mk({{ state: 'busy',
                last_activity_at: 2000, seen_at: 1000 }}),
              fresh: mk({{ state: 'idle',
                last_activity_at: 1000, seen_at: 1000 }}),
              finishedUnseen: mk({{ state: 'finished',
                last_activity_at: 3000, seen_at: 1000 }}),
            }};
          }}
        """)
        assert result["idleUnseen"]["hasUnseenClass"], "idle+未读应有 .unseen"
        assert result["idleUnseen"]["dotIsBlue"], (
            f"idle+未读: state-dot 应为蓝, 实际 bg={result['idleUnseen']['bg']}"
        )
        assert not result["idleUnseen"]["separateDotExists"], (
            "不应有独立的 .unseen-dot 元素 (已合到 state-dot)"
        )
        assert not result["idleSeen"]["hasUnseenClass"], "已读不应有 .unseen"
        assert not result["idleSeen"]["dotIsBlue"], (
            f"idle+已读: state-dot 不应为蓝, 实际 bg={result['idleSeen']['bg']}"
        )
        assert not result["busy"]["hasUnseenClass"], "busy 不显示蓝点"
        assert not result["busy"]["dotIsBlue"], (
            f"busy: state-dot 应为绿, 实际 bg={result['busy']['bg']}"
        )
        assert not result["fresh"]["hasUnseenClass"], "没跑过不显示蓝点"
        assert result["finishedUnseen"]["hasUnseenClass"], "finished+未读应有 .unseen"
        assert result["finishedUnseen"]["dotIsBlue"], (
            f"finished+未读: state-dot 应为蓝, "
            f"实际 bg={result['finishedUnseen']['bg']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_entering_chat_clears_dot(logged_in_page, base_url, test_token):
    """进 chat → 乐观清蓝点 + POST /seen → 卡片 .unseen 消失."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "dot-clear")
    try:
        _enter_home(page)
        page.wait_for_timeout(300)
        # 造一个未读态
        page.evaluate(f"""
          () => {{
            const sid = "{sid}";
            const cur = state.sessionsById.get(sid) || {{ id: sid }};
            state.sessionsById.set(sid, Object.assign({{}}, cur, {{
              state: 'idle', last_activity_at: Date.now()/1000,
              seen_at: (Date.now()/1000) - 100,
            }}));
            renderSessionList();
          }}
        """)
        card = page.locator(f".session-card[data-id='{sid}']")
        expect(card).to_have_class(re.compile(r"\bunseen\b"), timeout=2000)
        # 点开 → 应清掉
        card.click()
        expect(page.locator("body")).to_have_class(
            re.compile(r"\bhas-session\b"), timeout=5000)
        page.wait_for_timeout(300)
        # 本地 seen_at 应被乐观提到 last_activity_at
        cleared = page.evaluate(f"""
          () => {{
            const s = state.sessionsById.get("{sid}");
            return s && (s.seen_at || 0) >= (s.last_activity_at || 0);
          }}
        """)
        assert cleared, "进 chat 后本地 seen_at 应 >= last_activity_at (蓝点清)"
    finally:
        api_delete_session(base_url, test_token, sid)
