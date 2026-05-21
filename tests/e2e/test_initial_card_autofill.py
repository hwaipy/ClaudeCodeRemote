"""§4 首次加载策略 (终态):

- 进 chat 立即显示 chat-loading overlay (转圈圈遮挡 chat-log)
- 后台 backlog 推 INITIAL_MIN=40 raw events (server) + autoFillInitialCards
  静默续拉到 chat-log.scrollHeight ≥ 2 × window.innerHeight 或拉到顶
- autoFill 完成 → fade-out chat-loading overlay, 用户一次性看到完整内容
- 用户主动上拉到顶才触发 loadEarlierHistory (含 #history-loader 转圈反馈)
"""
from __future__ import annotations

import inspect
import pathlib
import re

from claude_code_remote.server import session_manager as sm


def test_initial_min_is_40():
    """backend INITIAL_MIN 应该是 40."""
    src = inspect.getsource(sm)
    m = re.search(r"INITIAL_MIN\s*=\s*(\d+)", src)
    assert m and int(m.group(1)) == 40, (
        f"INITIAL_MIN expected 40, got {m.group(1) if m else None}"
    )


def test_autofill_uses_viewport_height_criterion():
    """autoFillInitialCards 必须按 scrollHeight ≥ 2 × innerHeight 续拉,
    不再按"卡数"算. INITIAL_TARGET_CARDS 常量已删."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "INITIAL_TARGET_CARDS" not in src, (
        "INITIAL_TARGET_CARDS constant must be removed (用 viewport-height 替代)"
    )
    m = re.search(
        r"async function autoFillInitialCards\([^)]*\)\s*\{(.*?)\n\}",
        src, re.S,
    )
    assert m, "autoFillInitialCards body not found"
    body = m.group(1)
    assert "scrollHeight" in body and "innerHeight" in body, (
        f"autoFill must compare log.scrollHeight to window.innerHeight: {body[:300]}"
    )


def test_chat_loading_overlay_shows_then_autofill_fades():
    """enterChat cache-miss: chat-loading overlay 立即 show 遮挡, autoFill
    完成后才 fade-out 揭幕."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    # cache miss 路径必须 show overlay
    assert "_ld.hidden = false" in src, (
        "cache miss path must show chat-loading overlay first"
    )
    # autoFill finally 块负责 fade-out
    m = re.search(
        r"async function autoFillInitialCards\([^)]*\)\s*\{(.*?)\n\}",
        src, re.S,
    )
    assert m, "autoFillInitialCards not found"
    body = m.group(1)
    assert "chat-loading" in body and "fade-out" in body, (
        "autoFill finally must fade-out chat-loading overlay"
    )


def test_frontend_has_card_counter_and_autofill():
    """frontend 必须有 countVisibleChatCards + autoFillInitialCards. 后者
    在 backlog 渲完后静默续拉. selector 必须覆盖 4 类可见卡."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "function countVisibleChatCards" in src
    assert "function autoFillInitialCards" in src or \
           "async function autoFillInitialCards" in src
    for sel in (".bubble", ".tool-group", ".perm-card", ".askuser-card"):
        assert sel in src, f"selector missing: {sel}"


def test_autofill_uses_silent_loadearlier():
    """白盒: autoFillInitialCards 必须以 silent: true 调 loadEarlierHistory,
    否则用户会看到 history-loader 转圈, 体感"还在加载"."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(
        r"async function autoFillInitialCards\([^)]*\)\s*\{(.*?)\n\}",
        src, re.S,
    )
    assert m, "autoFillInitialCards body not found"
    body = m.group(1)
    assert re.search(r"loadEarlierHistory\(\s*\{\s*silent\s*:\s*true",
                     body), (
        f"autoFillInitialCards must call loadEarlierHistory({{silent: true}}): "
        f"{body[:300]}"
    )


def test_loadearlier_supports_silent_option():
    """白盒: loadEarlierHistory(opts) 接受 silent 选项, silent=true 时不
    调 setHistoryLoader."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(
        r"async function loadEarlierHistory\(([^)]*)\)\s*\{(.*?)^\}",
        src, re.S | re.M,
    )
    assert m, "loadEarlierHistory not found"
    args, body = m.group(1), m.group(2)
    assert "opts" in args or "options" in args or "silent" in args, (
        f"loadEarlierHistory must accept an options arg: ({args})"
    )
    # 所有 setHistoryLoader 调用必须在 !silent 守卫内 (或不在 fail/finish 路径)
    # 简化: body 必须包含 "if (!silent) setHistoryLoader" 至少一次
    assert re.search(r"if\s*\(\s*!\s*silent\s*\)\s*setHistoryLoader",
                     body), (
        "loadEarlierHistory must gate setHistoryLoader() behind !silent"
    )


def test_backlog_done_triggers_autofill():
    """白盒: backlog_done handler 必须调 autoFillInitialCards (静默 fill 到
    INITIAL_TARGET_CARDS)."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    idx = src.find('subtype === "backlog_done"')
    assert idx > 0, "backlog_done handler not found"
    chunk = src[idx:idx + 3000]
    assert "autoFillInitialCards" in chunk, (
        "backlog_done handler must trigger autoFillInitialCards"
    )


def test_count_visible_in_fragment_works_on_documentfragment(
    logged_in_page,
):
    """覆盖一个隐性 bug: 之前 countVisibleInFragment 用
    querySelectorAll(":scope > ...") 在 DocumentFragment 上返回 0 —
    :scope 在 fragment 上不可用. 结果 loadEarlierHistory 的 HARD_CAP 退出
    条件失效, 单次上拉跑满 MAX_BATCHES (~70 张卡而非 target=20)."""
    page = logged_in_page
    n = page.evaluate("""
      () => {
        const frag = document.createDocumentFragment();
        for (let i = 0; i < 7; i++) {
          const d = document.createElement('div');
          d.className = 'bubble user';
          frag.appendChild(d);
        }
        const g = document.createElement('div');
        g.className = 'tool-group';
        frag.appendChild(g);
        return countVisibleInFragment(frag);
      }
    """)
    assert n == 8, (
        f"countVisibleInFragment must count 7 bubbles + 1 tool-group = 8 "
        f"on a DocumentFragment (not 0 — that's the :scope-broken impl). "
        f"got {n}"
    )


def test_count_visible_cards_runtime(
    logged_in_page, base_url, test_token
):
    """运行时: countVisibleChatCards 数 4 类顶级卡, tool-card 嵌套不计."""
    from tests.helpers import api_spawn, api_delete_session
    sid = api_spawn(base_url, test_token, "/tmp", "card-count")
    try:
        from playwright.sync_api import expect
        page = logged_in_page
        card = page.locator(f"[data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        card.click()
        expect(page.locator("body")).to_have_class(
            re.compile(r"\bhas-session\b"), timeout=10000
        )
        page.wait_for_timeout(600)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            const b = document.createElement('div'); b.className = 'bubble user';
            log.appendChild(b);
            const g = document.createElement('div'); g.className = 'tool-group';
            g.appendChild(Object.assign(document.createElement('div'),
              { className: 'tool-card' }));
            g.appendChild(Object.assign(document.createElement('div'),
              { className: 'tool-card' }));
            log.appendChild(g);
            const p = document.createElement('div'); p.className = 'perm-card';
            log.appendChild(p);
            const a = document.createElement('div'); a.className = 'askuser-card';
            log.appendChild(a);
            return countVisibleChatCards();
          }
        """)
        assert result == 4, (
            f"countVisibleChatCards() should return 4 top-level cards "
            f"(tool-card inside tool-group not counted), got {result}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_autofill_keeps_going_when_batch_yields_zero_cards(
    logged_in_page, base_url, test_token
):
    """覆盖用户实测 bug: 进 chat 后看到 14 卡停下, autoFill 没继续拉.

    根因: silent loadEarlier 内部一轮可能拉了一大堆 raw events 但全是
    handleEvent 不产可见卡的 type (stream_event delta / 未识别 type). 旧
    autoFill 用 'now === prev → break' 判断, 一次 0 新增就 give up. 但
    db 里还有更早的 user_input/assistant 可拉.

    修法: autoFill 改用 state.firstSeq 是否前进作为"还能不能拉"的信号 —
    firstSeq 不动才是真拉不动. 本测试 stub 前若干轮返不产卡的 events
    但 first_seq 持续后退, 然后返 user_input. autoFill 必须能跨过这些
    无效轮, 最终到达 ≥ 20 卡."""
    from tests.helpers import api_spawn, api_delete_session
    sid = api_spawn(base_url, test_token, "/tmp", "autofill-noprogress")
    try:
        from playwright.sync_api import expect
        page = logged_in_page
        page.locator(f"[data-id='{sid}']").click()
        expect(page.locator("body")).to_have_class(
            re.compile(r"\bhas-session\b"), timeout=10000
        )
        page.wait_for_timeout(500)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            for (let i = 0; i < 5; i++) {
              const d = document.createElement('div');
              d.className = 'bubble user';
              d.textContent = 'seed ' + i;
              log.appendChild(d);
            }
            state.hasMoreHistory = true;
            state.firstSeq = 1000;
            state.loadingHistory = false;
            state.autoFilling = false;

            const origApi = window.api;
            let calls = 0;
            window.api = async (path) => {
              if (path.includes('/messages')) {
                calls += 1;
                const m = path.match(/before_seq=(\\d+)/);
                const before = m ? parseInt(m[1], 10) : 1000;
                // 前 10 次返不产卡的 events (handleEvent 不识别的 type),
                // first_seq 持续后退. 模拟"db 里这段全是 delta".
                if (calls <= 10) {
                  const msgs = [];
                  for (let i = 5; i >= 1; i--) {
                    msgs.push({
                      seq: before - i, ts: Date.now()/1000,
                      event: { type: '__noop__' },   // handleEvent 直接 return
                    });
                  }
                  return { messages: msgs, first_seq: before-5, has_more: true };
                }
                // 之后开始返 user_input
                const msgs = [];
                for (let i = 20; i >= 1; i--) {
                  msgs.push({
                    seq: before - i, ts: Date.now()/1000,
                    event: { type: 'user_input',
                             content: 'late ' + (before-i) },
                  });
                }
                return { messages: msgs, first_seq: before-20, has_more: true };
              }
              return origApi(path);
            };
            try { await autoFillInitialCards(); }
            finally { window.api = origApi; }

            return {
              calls,
              visible: countVisibleChatCards(),
              firstSeq: state.firstSeq,
            };
          }
        """)
        # autoFill 必须挺过 noop 阶段, 最终到达 ≥ 20 卡
        assert result["visible"] >= 20, (
            f"autoFill gave up too early: visible={result['visible']} "
            f"after {result['calls']} API calls. firstSeq={result['firstSeq']}. "
            f"Should have pushed past noop batches and reached 20+."
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_autofill_reaches_20_cards_via_stub(
    logged_in_page, base_url, test_token
):
    """运行时: 假设 chat-log 起始 3 张卡, hasMoreHistory=true, stub api 每次
    返 5 条 user_input. 调 autoFillInitialCards 后必须 ≥ 20 张可见卡 (循环
    续拉 4 次 = 20 + 3 = 23 张). 整个过程 #history-loader 始终保持 hidden."""
    from tests.helpers import api_spawn, api_delete_session
    sid = api_spawn(base_url, test_token, "/tmp", "autofill-20")
    try:
        from playwright.sync_api import expect
        page = logged_in_page
        page.locator(f"[data-id='{sid}']").click()
        expect(page.locator("body")).to_have_class(
            re.compile(r"\bhas-session\b"), timeout=10000
        )
        page.wait_for_timeout(500)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            for (let i = 0; i < 3; i++) {
              const d = document.createElement('div');
              d.className = 'bubble user';
              d.textContent = 'seed ' + i;
              log.appendChild(d);
            }
            state.hasMoreHistory = true;
            state.firstSeq = 1000;
            state.loadingHistory = false;
            state.autoFilling = false;

            // 监听 #history-loader 是否曾被显示 — autoFill 期间必须保持 hidden.
            const loader = document.getElementById('history-loader');
            let loaderShown = false;
            const obs = new MutationObserver(() => {
              if (loader && !loader.hidden) loaderShown = true;
            });
            obs.observe(loader, { attributes: true, attributeFilter: ['hidden'] });

            const origApi = window.api;
            window.api = async (path) => {
              if (path.includes('/messages')) {
                const m = path.match(/before_seq=(\\d+)/);
                const before = m ? parseInt(m[1], 10) : 1000;
                // 一次返 5 条 user_input (silent autoFill 会多轮拉直到 20)
                const msgs = [];
                for (let i = 5; i >= 1; i--) {
                  msgs.push({
                    seq: before - i, ts: Date.now()/1000,
                    event: { type: 'user_input', content: 'm' + (before-i) },
                  });
                }
                return {
                  messages: msgs,
                  first_seq: before - 5,
                  has_more: true,
                };
              }
              return origApi(path);
            };
            try { await autoFillInitialCards(); }
            finally { window.api = origApi; obs.disconnect(); }

            const visible = countVisibleChatCards();
            return { visible, loaderShown };
          }
        """)
        assert result["visible"] >= 20, (
            f"autoFill must reach ≥20 visible cards, got {result['visible']}"
        )
        assert not result["loaderShown"], (
            "history-loader must stay hidden during silent autoFill"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
