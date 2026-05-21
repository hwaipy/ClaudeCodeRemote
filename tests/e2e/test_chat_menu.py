"""§4 chat-head 右上角统一菜单按钮:

之前 chat-head 右半部散落 3 个交互入口:
  - #chat-perm (权限模式)
  - #chat-ctx-ring (ctx 用量环, 点 / hover 弹 #ctx-tooltip)
  - #ctx-tooltip 内的 ctx-model-select / ctx-effort-select

整合成单一菜单按钮 #chat-menu-btn + 弹层 #chat-menu (3 分区):
  - section-perm: <select> 4 选 (manual/accept_edits/plan/allow_all)
  - section-model: <select> 4 选 (Default/opus/sonnet/haiku)
  - section-ctx: 大字 pct + _fmtCtx(total)/_fmtCtx(limit) 详细

Effort 已从前端移除 — claude stream event 不报 effort, 默认值无法可靠探知;
后端 sess.effort 字段保留 (历史 session 不破坏), 但 UI 不暴露选项.

之前 3 个元素 (#chat-perm, #chat-ctx-ring, #ctx-tooltip) 必须从 DOM 移除,
避免双入口 + 重复同步.
"""
from __future__ import annotations

import re
import pathlib

import httpx
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    # 等 chat-loading overlay 真消失再返回 — 否则它会拦截 click
    expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
    page.wait_for_timeout(150)


# ---------- 白盒: HTML 结构 ----------

def test_chat_menu_btn_and_panel_exist():
    """白盒: index.html 含 #chat-menu-btn + #chat-menu (默认 hidden)."""
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="chat-menu-btn"' in html, "missing #chat-menu-btn button"
    assert 'id="chat-menu"' in html, "missing #chat-menu panel"
    # 3 个分区 (effort 已从前端移除)
    for sel_class in ("section-perm", "section-model", "section-ctx"):
        assert sel_class in html, f"missing chat-menu section: {sel_class}"
    assert "section-effort" not in html, (
        "section-effort must be removed (effort UI no longer exposed)"
    )


def test_old_chat_perm_btn_removed():
    """白盒: 旧的独立 #chat-perm 按钮已整合到 chat-menu, 必须从 DOM 移除.
    #chat-ctx-ring + #ctx-tooltip 切回保留 (放在 chat-menu 内 section-ctx)."""
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="chat-perm"' not in html, (
        "old #chat-perm button must be removed (integrated into chat-menu)"
    )
    # ring + tooltip 必须存在, 在 chat-menu 内
    assert 'id="chat-ctx-ring"' in html
    assert 'id="ctx-tooltip"' in html


def test_chat_menu_has_perm_button_and_menu():
    """白盒: section-perm 用 button (含 lock + lightning icon) + #perm-menu
    popup (4 个 .perm-menu-item, data-mode 覆盖 4 模式). 不再用 inline select."""
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="chat-menu-perm-btn"' in html, "must have #chat-menu-perm-btn"
    assert 'id="perm-menu"' in html, "must have #perm-menu popup"
    assert 'id="chat-menu-perm"' not in html, (
        "old #chat-menu-perm <select> must be removed"
    )
    # 4 个 perm-menu-item with data-mode
    for mode in ("manual", "accept_edits", "plan", "allow_all"):
        assert f'data-mode="{mode}"' in html, (
            f"perm-menu missing item for mode: {mode}"
        )
    # button 内含 manual + allowall 两套 SVG (icon 切换)
    btn_idx = html.find('id="chat-menu-perm-btn"')
    btn_chunk = html[btn_idx:btn_idx + 1500]
    assert "perm-svg-manual" in btn_chunk and "perm-svg-allow_all" in btn_chunk


def test_chat_menu_has_model_button_and_menu():
    """白盒: model 改 button + popup, 跟 perm 同款. 含 4 个 model-menu-item
    (data-model='' / opus / sonnet / haiku)."""
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="chat-menu-model-btn"' in html, "must have #chat-menu-model-btn"
    assert 'id="model-menu"' in html, "must have #model-menu popup"
    # 旧 select 应该没了
    assert 'id="chat-menu-model"' not in html, (
        "old #chat-menu-model <select> must be removed"
    )
    for val in ('data-model=""', 'data-model="opus"',
                'data-model="sonnet"', 'data-model="haiku"'):
        assert val in html, f"model-menu missing item: {val}"


def test_chat_menu_has_no_effort_select():
    """白盒: effort 已从前端移除, chat-menu 不应再含 #chat-menu-effort."""
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="chat-menu-effort"' not in html, (
        "#chat-menu-effort must be removed (effort UI dropped)"
    )


def test_default_option_annotates_with_cur_model(
    logged_in_page, base_url, test_token
):
    """运行时: state.sessionsById[sid].cur_model 非空时, Default 选项文本
    必须含 "currently: <cur_model>"; 空时只显示 "Default"."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "default-annot")
    try:
        _enter_chat(page, sid)
        # 注入 cur_model 模拟 stream event 已报回
        page.evaluate(f"""
          () => {{
            const s = state.sessionsById.get({sid!r});
            if (s) s.cur_model = 'claude-opus-4-7-20251201';
            refreshConvStatus();
          }}
        """)
        page.wait_for_timeout(80)
        opt_text = page.evaluate("""
          () => document.getElementById('model-menu-sub-default').textContent
        """)
        assert "claude-opus-4-7" in opt_text, (
            f"Default item sub should annotate cur_model: {opt_text!r}"
        )
        assert "currently" not in opt_text.lower(), (
            f"'currently' word must not appear: {opt_text!r}"
        )
        # 清空 cur_model 后退回简单 "CLI picks"
        page.evaluate(f"""
          () => {{
            const s = state.sessionsById.get({sid!r});
            if (s) s.cur_model = '';
            state.currentMsgModel = '';
            refreshConvStatus();
          }}
        """)
        page.wait_for_timeout(80)
        opt_text2 = page.evaluate("""
          () => document.getElementById('model-menu-sub-default').textContent
        """)
        assert "claude-opus-4-7" not in opt_text2, (
            f"sub should drop cur_model when cleared: {opt_text2!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_default_option_text_idempotent_across_refreshes(
    logged_in_page, base_url, test_token
):
    """覆盖闪烁 bug: refreshConvStatus 每秒跑一次, 必须只在 cur_model 真变化
    时才改写 Default option 的 textContent. 否则桌面浏览器在 select dropdown
    展开期间被无差别 textContent 写入会强制关闭 dropdown, 体感"一直闪烁".

    用 MutationObserver 计 textContent 变化次数: 同一 cur_model 下连续刷 5 次,
    必须 0 次 mutation."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "model-no-flicker")
    try:
        _enter_chat(page, sid)
        mutations = page.evaluate(f"""
          async () => {{
            const s = state.sessionsById.get({sid!r});
            if (s) s.cur_model = 'claude-opus-4-7-20251201';
            refreshConvStatus();   // 先刷一次, 让 textContent 同步到目标
            const defOpt = document.getElementById('model-menu-sub-default');
            let count = 0;
            const obs = new MutationObserver(muts => {{
              for (const m of muts) {{
                if (m.type === 'characterData' || m.type === 'childList') {{
                  count += 1;
                }}
              }}
            }});
            obs.observe(defOpt, {{
              characterData: true, childList: true, subtree: true,
            }});
            // cur_model 不变, refresh 5 次. 每次都不应触发任何 mutation.
            for (let i = 0; i < 5; i++) refreshConvStatus();
            await new Promise(r => setTimeout(r, 30));
            obs.disconnect();
            return count;
          }}
        """)
        assert mutations == 0, (
            f"Default option textContent mutated {mutations} times despite "
            f"cur_model not changing — will cause select flicker on desktop"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_cli_defaults_endpoint_returns_json(base_url, test_token):
    """GET /api/cli/defaults 返回 model + effort 两字段 (值可能 null)."""
    r = httpx.get(
        f"{base_url}/api/cli/defaults",
        headers={"Authorization": f"Bearer {test_token}"},
        timeout=5,
    )
    r.raise_for_status()
    j = r.json()
    assert "model" in j and "effort" in j, j
    # 值可能是 null 或字符串, 都接受 (取决于 settings.json 内容)
    for k in ("model", "effort"):
        assert j[k] is None or isinstance(j[k], str)


def test_chat_menu_inline_on_wide_viewport(
    playwright, base_url, test_token
):
    """桌面宽屏 (≥900px): #chat-menu 必须 inline 显示在 chat-head 内,
    #chat-menu-btn 隐藏. 不点按钮也能直接看到 perm/model/ctx."""
    from tests.helpers import api_spawn, api_delete_session
    browser = playwright.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    try:
        page.goto(base_url)
        page.fill("#login-token", test_token)
        page.click("#login-go")
        page.wait_for_selector("#view-home", timeout=5000)
        sid = api_spawn(base_url, test_token, "/tmp", "wide-inline")
        try:
            page.locator(f"[data-id='{sid}']").click()
            expect(page.locator("body")).to_have_class(
                re.compile(r"\bhas-session\b"), timeout=10000
            )
            expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
            page.wait_for_timeout(150)
            # 宽屏下 menu 必须 inline 可见 (不靠点按钮)
            menu_disp = page.evaluate(
                "() => getComputedStyle(document.getElementById("
                "'chat-menu')).display"
            )
            assert menu_disp != "none", (
                f"#chat-menu must be visible inline on wide viewport, "
                f"got display={menu_disp}"
            )
            # menu-btn 隐藏
            btn_disp = page.evaluate(
                "() => getComputedStyle(document.getElementById("
                "'chat-menu-btn')).display"
            )
            assert btn_disp == "none", (
                f"#chat-menu-btn must be hidden on wide viewport, got {btn_disp}"
            )
            # 3 分区都可点 — perm button 弹 menu, 选 plan
            page.locator("#chat-menu-perm-btn").click()
            page.wait_for_timeout(80)
            page.locator('.perm-menu-item[data-mode="plan"]').click()
            page.wait_for_timeout(300)
            import httpx
            r = httpx.get(
                f"{base_url}/api/sessions/{sid}/permission_mode",
                headers={"Authorization": f"Bearer {test_token}"},
                timeout=5,
            )
            r.raise_for_status()
            assert r.json().get("mode") == "plan"
        finally:
            api_delete_session(base_url, test_token, sid)
    finally:
        ctx.close(); browser.close()


def test_chat_menu_inline_on_narrow_viewport_too(
    playwright, base_url, test_token
):
    """窄屏 (<900px) 也 inline 展开 — 4 个 icon 直接在 chat-head 显示,
    #chat-menu-btn (⋯) 隐藏, 没有 popup 弹层模式."""
    from tests.helpers import api_spawn, api_delete_session
    browser = playwright.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True, is_mobile=True,
    )
    page = ctx.new_page()
    try:
        page.goto(base_url)
        page.fill("#login-token", test_token)
        page.click("#login-go")
        page.wait_for_selector("#view-home", timeout=5000)
        sid = api_spawn(base_url, test_token, "/tmp", "narrow-inline")
        try:
            page.locator(f"[data-id='{sid}']").click()
            expect(page.locator("body")).to_have_class(
                re.compile(r"\bhas-session\b"), timeout=10000
            )
            expect(page.locator("#chat-loading")).to_be_hidden(timeout=5000)
            page.wait_for_timeout(150)
            btn_disp = page.evaluate(
                "() => getComputedStyle(document.getElementById("
                "'chat-menu-btn')).display"
            )
            assert btn_disp == "none", (
                f"#chat-menu-btn must hide on narrow too, got {btn_disp}"
            )
            menu_disp = page.evaluate(
                "() => getComputedStyle(document.getElementById("
                "'chat-menu')).display"
            )
            assert menu_disp != "none", (
                f"#chat-menu must be inline on narrow, got {menu_disp}"
            )
        finally:
            api_delete_session(base_url, test_token, sid)
    finally:
        ctx.close(); browser.close()


def test_status_payload_exposes_cur_model(base_url, test_token):
    """白盒后端: status_payload 必须含 cur_model 字段 (前端用它注释 Default)."""
    from tests.helpers import api_list_sessions
    sid = api_spawn(base_url, test_token, "/tmp", "cur-model-key")
    try:
        rows = api_list_sessions(base_url, test_token)
        match = [r for r in rows if r["id"] == sid]
        assert match, sid
        assert "cur_model" in match[0], (
            f"status_payload missing cur_model: {match[0]}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_menu_ctx_uses_ring_and_tooltip():
    """白盒: ctx 切回 SVG 圆环 #chat-ctx-ring + #ctx-tooltip (跟早期一模一样).
    不再用 #chat-menu-ctx-pct / -detail 内嵌文字."""
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    assert 'id="chat-ctx-ring"' in html, "must restore #chat-ctx-ring SVG"
    assert 'id="ctx-tooltip"' in html, "must restore #ctx-tooltip popover"
    assert 'id="ctx-tooltip-pct"' in html
    assert 'id="ctx-tooltip-detail"' in html
    assert 'id="chat-menu-ctx-pct"' not in html, (
        "old inline chat-menu-ctx-pct must be removed (replaced by ring)"
    )
    assert 'id="chat-menu-ctx-detail"' not in html


def test_chat_menu_sections_use_svg_icons():
    """白盒: 各 section 用 SVG icon 替代文字 label. perm 用 .chat-menu-perm-btn
    内嵌 lock/lightning SVG; model 用 .chat-menu-model-btn 内嵌 chip SVG."""
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    # model section 用 .chat-menu-model-btn
    midx = html.find('class="chat-menu-section section-model"')
    assert midx > 0
    mchunk = html[midx:midx + 3000]
    assert "chat-menu-model-btn" in mchunk, (
        "section-model must use .chat-menu-model-btn"
    )
    assert "<svg" in mchunk, "section-model must contain an SVG icon"
    # perm section 用 .chat-menu-perm-btn (含两套 perm-svg)
    pidx = html.find('class="chat-menu-section section-perm"')
    assert pidx > 0
    pchunk = html[pidx:pidx + 3000]
    assert "chat-menu-perm-btn" in pchunk
    assert "perm-svg-manual" in pchunk and "perm-svg-allow_all" in pchunk
    # 旧文字 label 删了
    assert ">Permission</div>" not in html
    assert ">Model</div>" not in html
    assert ">Context usage</div>" not in html


# ---------- 运行时 ----------

def test_chat_menu_initially_hidden(
    logged_in_page, base_url, test_token
):
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "menu-hidden")
    try:
        _enter_chat(page, sid)
        is_hidden = page.evaluate(
            "() => document.getElementById('chat-menu').hidden"
        )
        assert is_hidden, "chat-menu must start hidden"
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_menu_perm_item_patches_mode(
    logged_in_page, base_url, test_token
):
    """点 #chat-menu-perm-btn 弹 #perm-menu, 点 data-mode='plan' item →
    PUT permission_mode → 后端模式更新, button 切到 plan (icon 仍 lock 因为
    非 allow_all)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "menu-perm-item")
    try:
        _enter_chat(page, sid)
        # 点 perm button 弹 menu
        page.locator("#chat-menu-perm-btn").click()
        page.wait_for_timeout(80)
        assert not page.evaluate(
            "() => document.getElementById('perm-menu').hidden"
        ), "perm-menu must show after btn click"
        # 点 plan item
        page.locator('.perm-menu-item[data-mode="plan"]').click()
        page.wait_for_timeout(300)
        # menu close
        assert page.evaluate(
            "() => document.getElementById('perm-menu').hidden"
        ), "perm-menu must close after item click"
        r = httpx.get(
            f"{base_url}/api/sessions/{sid}/permission_mode",
            headers={"Authorization": f"Bearer {test_token}"},
            timeout=5,
        )
        r.raise_for_status()
        assert r.json().get("mode") == "plan", r.json()
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_menu_perm_btn_mode_class_and_title(
    logged_in_page, base_url, test_token
):
    """每个模式都有独立 icon class + button.title 跟随刷新 (hover 显示)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "perm-icon")
    try:
        _enter_chat(page, sid)
        for mode, label in [
            ("allow_all", "Allow all"),
            ("plan", "Plan only"),
            ("accept_edits", "Auto edits"),
            ("manual", "Ask each time"),
        ]:
            page.locator("#chat-menu-perm-btn").click()
            page.wait_for_timeout(50)
            page.locator(f'.perm-menu-item[data-mode="{mode}"]').click()
            page.wait_for_timeout(300)
            btn = page.locator("#chat-menu-perm-btn")
            cls = btn.get_attribute("class") or ""
            assert f"mode-{mode}" in cls, (
                f"button must have mode-{mode} class: {cls}"
            )
            # 其它 3 个 mode class 不应同时存在
            for other in ("manual", "accept_edits", "plan", "allow_all"):
                if other == mode: continue
                assert f"mode-{other}" not in cls, (
                    f"only one mode class at a time; saw mode-{other}: {cls}"
                )
            title = btn.get_attribute("title") or ""
            assert label in title, (
                f"button title should show '{label}', got: {title!r}"
            )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_menu_has_four_perm_icons():
    """白盒: 4 个模式各自 SVG (manual/accept_edits/plan/allow_all)."""
    import pathlib
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    btn_idx = html.find('id="chat-menu-perm-btn"')
    chunk = html[btn_idx:btn_idx + 3000]
    for mode in ("manual", "accept_edits", "plan", "allow_all"):
        assert f"perm-svg-{mode}" in chunk, (
            f"perm button missing icon SVG for mode: {mode}"
        )


def test_chat_menu_model_select_patches_session(
    logged_in_page, base_url, test_token
):
    """点 #chat-menu-model-btn 弹 #model-menu, 点 sonnet item → PATCH
    /model_effort → 后端 session.model 更新."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "menu-model")
    try:
        _enter_chat(page, sid)
        page.locator("#chat-menu-model-btn").click()
        page.wait_for_timeout(80)
        page.locator('.model-menu-item[data-model="sonnet"]').click()
        page.wait_for_timeout(400)
        from tests.helpers import api_list_sessions
        rows = api_list_sessions(base_url, test_token)
        match = [r for r in rows if r["id"] == sid]
        assert match and match[0]["model"] == "sonnet", (
            f"PATCH didn't update server-side model: {match}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_menu_ctx_ring_reflects_percentage(
    logged_in_page, base_url, test_token
):
    """运行时: 注入 lastInputTotal=50% of contextLimit, refreshConvStatus
    后 #chat-ctx-ring 的 .fill stroke-dashoffset 必须约等于 C/2 (圆周一半).
    C = 2π·10 ≈ 62.83. 50% → offset ≈ 31.4."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "ring-pct")
    try:
        _enter_chat(page, sid)
        page.evaluate("""
          () => {
            state.lastInputTotal = 100000;
            state.contextLimit = 200000;
            refreshConvStatus();
          }
        """)
        page.wait_for_timeout(50)
        offset = page.evaluate(
            "() => parseFloat(document.querySelector("
            "'#chat-ctx-ring .fill').style.strokeDashoffset)"
        )
        # C/2 ≈ 31.42, 给 ±1 容差
        assert 30 <= offset <= 33, (
            f"50% ctx → stroke-dashoffset ≈ 31.42, got {offset}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_menu_ctx_tooltip_shows_on_hover_or_touch(
    logged_in_page, base_url, test_token
):
    """ctx-ring 桌面 hover (mouseenter) 弹 tooltip; 触屏 touchstart 一次弹.
    Ring 不再响应 click (用户不希望桌面端可点击)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "ring-tooltip")
    try:
        _enter_chat(page, sid)
        page.evaluate("""
          () => {
            state.lastInputTotal = 100000;
            state.contextLimit = 200000;
            refreshConvStatus();
          }
        """)
        page.wait_for_timeout(50)
        assert page.evaluate(
            "() => document.getElementById('ctx-tooltip').hidden"
        ), "tooltip starts hidden"
        # 桌面: dispatch mouseenter
        page.locator("#chat-ctx-ring").dispatch_event("mouseenter")
        page.wait_for_timeout(80)
        assert not page.evaluate(
            "() => document.getElementById('ctx-tooltip').hidden"
        ), "tooltip should show on hover"
        pct_text = page.locator("#ctx-tooltip-pct").text_content() or ""
        detail_text = page.locator("#ctx-tooltip-detail").text_content() or ""
        assert "50" in pct_text and "%" in pct_text, pct_text
        assert "100" in detail_text and "200" in detail_text, detail_text
        assert "k" in detail_text or "M" in detail_text, detail_text
    finally:
        api_delete_session(base_url, test_token, sid)


def test_chat_ctx_ring_not_clickable_or_focusable():
    """白盒: ring 不应有 role=button / tabindex=0 (不可聚焦, 没 focus 边框).
    JS 也不该再注册 click handler 直接 toggle tooltip."""
    import pathlib
    html = pathlib.Path(
        "claude_code_remote/server/static/index.html"
    ).read_text()
    ring_idx = html.find('id="chat-ctx-ring"')
    chunk = html[ring_idx:ring_idx + 400]
    assert 'role="button"' not in chunk, "ring must not be role=button"
    assert "tabindex" not in chunk, "ring must not be focusable (tabindex)"


