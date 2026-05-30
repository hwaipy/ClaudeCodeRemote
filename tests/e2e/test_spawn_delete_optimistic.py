"""Spawn / Delete 乐观更新 — session list 不等 WS 推, 立刻反映用户意图.

桌面双栏 (wide) 下 home 视图始终可见, 用户期望:
  - 点 Start → 新卡立刻出现在 list (即使 WS session_state 还没到)
  - 点 Delete → 卡片立刻消失 (即使 WS session_deleted 还没到)

实现: spawn 成功后乐观写 state.sessionsById + renderSessionList;
       delete 前乐观 remove + 失败回滚.

测试方式: 拦截 handleGlobalMsg 让 session_state / session_deleted 全 drop,
        然后看 UI 是否仍然立即反映 — 这样能精确隔离"乐观渲染 vs WS 推渲染".
"""
from __future__ import annotations

import re

import httpx
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn, api_list_sessions


def _enter_home_wide(page, base_url):
    """logged_in page goto + 等 home 出现. wide_page fixture 已 goto."""
    expect(page.locator("#view-home")).to_be_visible(timeout=10000)
    page.wait_for_timeout(200)


def _block_global_ws_session_events(page):
    """Hook handleGlobalMsg 丢弃 session_state / session_deleted / snapshot,
    其余正常 (避免 WS push 修补乐观 UI)."""
    page.evaluate("""
      () => {
        if (window.__origHandleGlobalMsg) return;
        window.__origHandleGlobalMsg = window.handleGlobalMsg;
        window.handleGlobalMsg = (msg) => {
          if (msg && (msg.type === 'session_state'
                      || msg.type === 'session_deleted'
                      || msg.type === 'snapshot')) {
            return;   // drop — 让乐观渲染独自出场
          }
          return window.__origHandleGlobalMsg(msg);
        };
      }
    """)


def test_spawn_card_appears_immediately_wide(wide_page, base_url, test_token):
    """桌面双栏: spawn 完 list 立刻显示新卡, 不等 WS."""
    page = wide_page
    _enter_home_wide(page, base_url)
    _block_global_ws_session_events(page)

    # 记录已有 session id 集合
    before_ids = set(page.evaluate(
        "() => Array.from(state.sessionsById.keys())"
    ))

    # 直接调 spawn API + 走前端落地路径 (跟点 Start 等价).
    # 等价于按 #spawn-go: 但走 UI 太脆易受 modal 状态影响, 这里直接复用
    # 真实 fetch 路径 — 验证乐观写入 sessionsById.
    new_id = page.evaluate("""
      async () => {
        const r = await api('/api/spawn', {
          method: 'POST',
          body: JSON.stringify({
            cwd: '/tmp', name: 'optspawn',
            permission_mode: 'manual', model: '',
          }),
        });
        // 触发跟 #spawn-go click handler 一样的乐观更新
        if (window._optimisticSpawnAdd) window._optimisticSpawnAdd(r);
        return r.id;
      }
    """)

    # 立刻 (无 page.wait_for_timeout) 检查卡片已在 DOM
    card = page.locator(f".session-card[data-id='{new_id}']")
    expect(card).to_have_count(1, timeout=1000)
    expect(card).to_be_visible(timeout=1000)

    # 且 sessionsById 里有它
    has = page.evaluate(f"() => state.sessionsById.has('{new_id}')")
    assert has, "sessionsById 应包含乐观写入的新 session"
    assert new_id not in before_ids

    # cleanup
    try:
        api_delete_session(base_url, test_token, new_id)
    except Exception:
        pass


def test_delete_card_disappears_immediately_wide(
    wide_page, base_url, test_token,
):
    """桌面双栏: 删卡片立刻消失, 不等 WS session_deleted."""
    page = wide_page
    _enter_home_wide(page, base_url)

    # 先种一个 session 让它显示
    sid = api_spawn(base_url, test_token, "/tmp", "tobedel")
    try:
        # 等 WS snapshot/session_state 推进来, 卡片出现
        page.wait_for_selector(f".session-card[data-id='{sid}']", timeout=5000)

        # 再装拦截器, 阻断后续 WS 推送
        _block_global_ws_session_events(page)

        # 触发删除路径 (跟点 menu → Delete 同效)
        page.evaluate(f"""
          () => {{
            window.__deleteResult = (async () => {{
              if (window._optimisticSessionDelete) {{
                window._optimisticSessionDelete('{sid}');
              }}
              try {{
                await api('/api/sessions/{sid}', {{ method: 'DELETE' }});
                return 'ok';
              }} catch (e) {{
                return 'err:' + (e.message || e);
              }}
            }})();
          }}
        """)

        # 卡片应立刻消失 — 不靠 server / WS
        expect(page.locator(f".session-card[data-id='{sid}']")).to_have_count(
            0, timeout=1000
        )
        # state.sessionsById 也应该已删
        has = page.evaluate(f"() => state.sessionsById.has('{sid}')")
        assert not has, "sessionsById 应已乐观 delete"

        # 等 fetch 完成 (避免 leak)
        page.evaluate("() => window.__deleteResult")
    finally:
        try:
            api_delete_session(base_url, test_token, sid)
        except Exception:
            pass


def test_delete_failure_rolls_back(wide_page, base_url, test_token):
    """删除 API 失败 → 乐观 remove 必须回滚 (卡又出现)."""
    page = wide_page
    _enter_home_wide(page, base_url)

    sid = api_spawn(base_url, test_token, "/tmp", "del-rollback")
    try:
        page.wait_for_selector(f".session-card[data-id='{sid}']", timeout=5000)
        _block_global_ws_session_events(page)

        # 让 fetch 给 500 错: monkey-patch api() 仅这一次
        page.evaluate(f"""
          () => {{
            const orig = window.api;
            window.api = async (path, opts) => {{
              if (path === '/api/sessions/{sid}'
                  && (opts || {{}}).method === 'DELETE') {{
                throw new Error('simulated 500');
              }}
              return orig(path, opts);
            }};
          }}
        """)

        # 触发 menu → delete (跳 confirm)
        page.evaluate(f"""
          async () => {{
            const sid = '{sid}';
            if (window._optimisticSessionDelete) {{
              window._optimisticSessionDelete(sid);
            }}
            try {{
              await api(`/api/sessions/${{sid}}`, {{ method: 'DELETE' }});
            }} catch (e) {{
              // 应触发回滚
              if (window._optimisticSessionDeleteRollback) {{
                await window._optimisticSessionDeleteRollback(sid, e);
              }}
            }}
          }}
        """)
        # 回滚后卡片应再次出现 (从 server 或缓存)
        page.wait_for_selector(
            f".session-card[data-id='{sid}']", timeout=5000)
    finally:
        try:
            # 直接调真 API 删 (不通过 page.api 的 monkey 版本)
            httpx.delete(
                f"{base_url}/api/sessions/{sid}",
                headers={"Authorization": f"Bearer {test_token}"},
                timeout=5,
            )
        except Exception:
            pass
