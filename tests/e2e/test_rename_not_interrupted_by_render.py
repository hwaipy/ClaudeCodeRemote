"""bug repro: session list 中, 用户在 rename 一个 session (contenteditable
正在编辑), 此时 WS session_state event 到达 → handleGlobalMsg 调
renderSessionList → 整个 list innerHTML 重置 → 用户输入框失焦 + 内容丢失.
"""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_home(page):
    # 等 home 加载完, session list 渲了
    expect(page.locator("#view-home")).to_be_visible(timeout=10000)
    page.wait_for_timeout(150)


def test_rename_survives_session_state_update(
    logged_in_page, base_url, test_token
):
    """场景: 用户点 ⋯ → Rename 进入编辑态, 正在打字, 此时全局 WS 推一条
    session_state event (例如另一个 session 的活动 / 当前 session 的状态变).
    期望: 编辑框还在 focus, 文字还在, 用户可以继续输入和提交."""
    page = logged_in_page
    sid_a = api_spawn(base_url, test_token, "/tmp", "session-A-original")
    sid_b = api_spawn(base_url, test_token, "/tmp", "session-B-other")
    try:
        _enter_home(page)

        # 1. 找到 A 卡, 模拟 click ⋯ → rename. 直接调内部的 startRename 太
        #    黑盒, 走真实交互: 点 menu btn → 点 Rename item.
        card_a = page.locator(f".session-card[data-id='{sid_a}']")
        expect(card_a).to_be_visible(timeout=5000)
        card_a.locator(".card-menu-btn").click()
        card_a.locator(".card-menu-item[data-action='rename']").click()
        nameEl = card_a.locator(".name.editing")
        expect(nameEl).to_be_visible(timeout=2000)

        # 2. 全选 + 改成新名字 (典型用户操作: select-all + 直接输入覆盖)
        page.keyboard.press("Meta+A" if False else "Control+A")
        page.keyboard.type("renamed-A-")
        page.wait_for_timeout(100)
        typed_so_far = nameEl.text_content()
        assert "renamed-A-" in (typed_so_far or ""), (
            f"setup failed — typed text not visible: {typed_so_far!r}"
        )

        # 3. 用户还在打字, 这时模拟 WS 推一条 session_state for B 进 state +
        #    渲染. 直接走 page.evaluate 调 handleGlobalMsg, 跟真实 WS 路径
        #    完全一样.
        page.evaluate(f"""
          () => {{
            const existing = state.sessionsById.get("{sid_b}") || {{}};
            handleGlobalMsg({{
              type: "session_state",
              id: "{sid_b}",
              state: "busy",
              name: "session-B-other",
              cwd: "/tmp",
              last_activity_at: Date.now() / 1000,
              created_at: existing.created_at || (Date.now() / 1000 - 100),
            }});
          }}
        """)
        page.wait_for_timeout(200)

        # 4. 验证 A 的编辑态没被打断
        still_editing = card_a.locator(".name.editing")
        expect(still_editing).to_be_visible(timeout=1000)
        # 同一 DOM element 还在 — focus 没丢
        is_focused = page.evaluate(f"""
          () => {{
            const card = document.querySelector(
              ".session-card[data-id='{sid_a}']");
            const nameEl = card?.querySelector(".name");
            return {{
              hasFocus: document.activeElement === nameEl,
              text: nameEl?.textContent || "",
              editingClass: !!nameEl?.classList.contains("editing"),
            }};
          }}
        """)
        assert is_focused["editingClass"], (
            "rename 编辑态被中断 — .editing class 丢了"
        )
        assert "renamed-A-" in is_focused["text"], (
            f"输入文字丢失: {is_focused['text']!r}"
        )
        assert is_focused["hasFocus"], (
            "输入框 focus 丢失 — 用户继续打字会落在别处"
        )

        # 5. 用户继续打字, 然后 Enter 提交
        page.keyboard.type("done")
        page.keyboard.press("Enter")
        # 等 API + optimistic update 落地
        page.wait_for_timeout(500)
        new_name_in_state = page.evaluate(f"""
          () => state.sessionsById.get("{sid_a}")?.name || ""
        """)
        assert new_name_in_state == "renamed-A-done", (
            f"最终重命名应是 'renamed-A-done', got {new_name_in_state!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid_a)
        api_delete_session(base_url, test_token, sid_b)
