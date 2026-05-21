"""§4 上拉加载更早历史的契约:

- prepend 期间不能 removeChild 已有节点再 appendChild 回去 (会让锚位移
  闪烁 + DOM 引用失效)
- 用 scrollHeight 差值锚定 scrollTop, 不用多轮 getBoundingClientRect
  纠偏
- 渲染期间隔离 state.currentToolGroup (earlier 批不并入 recent 批的组)
- loader 在请求期间显示, 结束隐藏
- **一次"加载更早"目标 ≥ HISTORY_VISIBLE_TARGET (=10) 张可见卡**, 内部
  循环拉多批 → 边渲染到 detached fragment 边数卡 → 满足后一次性 prepend.
  如果拉到的最老一张可见卡是 .tool-group, 必须继续拉直到顶端不是
  tool-group (避免顶端被截断的工具组), 除非 has_more=false.
- 跨批 tool-group 合并: 旧批 fragment 末尾的 tool-group 跟累积 fragment
  顶部的 tool-group 在 DOM 上是同一段连续 tool_use → 把累积 fragment
  顶部的 tool-cards 合并到旧批 fragment 末尾的 tool-group.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import httpx
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn


def _enter_chat(page, sid):
    page.locator(f"[data-id='{sid}']").click()
    expect(page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    page.wait_for_timeout(500)


def _load_fn_body():
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(
        r"async function loadEarlierHistory\([^)]*\)\s*\{(.*?)^\}",
        src, re.S | re.M,
    )
    assert m, "loadEarlierHistory not found"
    return m.group(1)


def test_loadearlier_uses_earlierFragment_pattern():
    """白盒: loadEarlierHistory 必须用 detached DocumentFragment 渲染
    earlier 批, 然后一次 insertBefore. 禁止 removeChild 所有现有节点
    再 appendChild 回来这种 destructive 做法."""
    body = _load_fn_body()
    assert "earlierFragment" in body or "DocumentFragment" in body, (
        "loadEarlierHistory should render into a detached fragment, "
        "not directly into chat-log"
    )
    assert "insertBefore" in body, (
        "loadEarlierHistory should prepend the fragment via insertBefore"
    )
    assert "while (log.firstChild)" not in body, (
        "loadEarlierHistory must NOT strip + re-render existing children — "
        "use detached fragment + insertBefore instead"
    )


def test_loadearlier_uses_scrollheight_delta_not_drift_loop():
    """白盒: 滚动锚定用 scrollHeight 差值, 不用多轮 getBoundingClientRect."""
    body = _load_fn_body()
    assert "for (let i = 0; i < 6" not in body, (
        "6-iteration drift loop is jank-prone — use scrollHeight delta"
    )
    assert "scrollHeight" in body, (
        "loadEarlierHistory must compute scroll anchor via scrollHeight delta"
    )


def test_loadearlier_saves_and_restores_tool_group():
    """白盒: 渲染 earlier 批前要把 state.currentToolGroup 设 null 隔离."""
    body = _load_fn_body()
    assert "currentToolGroup" in body, (
        "loadEarlierHistory must reset / restore state.currentToolGroup "
        "during earlier-batch rendering"
    )


def test_loadearlier_has_visible_target_constant():
    """白盒: HISTORY_VISIBLE_TARGET=20 (单次上拉目标 20 张可见卡)."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(r"HISTORY_VISIBLE_TARGET\s*=\s*(\d+)", src)
    assert m, "HISTORY_VISIBLE_TARGET constant must exist"
    assert int(m.group(1)) == 20, (
        f"HISTORY_VISIBLE_TARGET should be 20, got {m.group(1)}"
    )
    body = _load_fn_body()
    assert "HISTORY_VISIBLE_TARGET" in body


def test_loadearlier_has_visible_hard_cap():
    """白盒: HISTORY_VISIBLE_HARD_CAP > TARGET 且 ≤ 1.6×TARGET — 上限不能
    超出 target 太多, 否则单次上拉刷出几十张. 当前 TARGET=20, CAP=30."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(r"HISTORY_VISIBLE_HARD_CAP\s*=\s*(\d+)", src)
    assert m, "HISTORY_VISIBLE_HARD_CAP constant must exist"
    cap = int(m.group(1))
    target = int(re.search(r"HISTORY_VISIBLE_TARGET\s*=\s*(\d+)", src).group(1))
    assert cap > target, f"HARD_CAP ({cap}) must be > TARGET ({target})"
    assert cap <= int(target * 1.6), (
        f"HARD_CAP ({cap}) must be ≤ 1.6×TARGET ({target}) — keep upper "
        f"bound tight so single-pull is not flooding the screen"
    )
    body = _load_fn_body()
    assert "HISTORY_VISIBLE_HARD_CAP" in body


def test_loadearlier_loops_until_target_or_top():
    """白盒: loadEarlierHistory 必须用 while/for 循环拉多批, 每批渲染到
    fragment, 满足"≥ target 卡 且 顶端不是 tool-group" 或 !has_more 时
    才 break. 关键判据: 检查 fragment.firstElementChild 是否 .tool-group."""
    body = _load_fn_body()
    # 必须有循环
    assert re.search(r"\bwhile\s*\(", body) or re.search(r"\bfor\s*\(", body), (
        "loadEarlierHistory must contain a loop to pull multiple batches"
    )
    # 必须检查 firstElementChild + tool-group 类
    assert (
        re.search(r"firstElementChild", body)
        and re.search(r"tool-group", body)
    ), (
        "loadEarlierHistory must inspect fragment.firstElementChild for "
        "the .tool-group class to detect a truncated head"
    )


def test_loadearlier_merges_tool_groups_across_batches():
    """白盒: 跨批合并 — 当前批 batchFrag 最末是 tool-group 且累积 workFrag
    顶端也是 tool-group, 必须把后者的 tool-cards 移到前者 (.tool-group-body),
    然后删 workFrag 顶端那个空的 tool-group. 否则视觉上同一段连续 tool_use
    被切成两个相邻 group."""
    body = _load_fn_body()
    # 合并的关键字: 既要看 lastElementChild (batch 末尾) 又要看 firstElementChild
    # (workFrag 顶端). 两者都要是 tool-group, 然后才移子节点
    assert "lastElementChild" in body, (
        "tool-group merge across batches needs lastElementChild check"
    )


def test_loadearlier_preserves_anchor_within_2px(
    logged_in_page, base_url, test_token
):
    """运行时: 注入足够多原始历史让 has_more=true, 进 chat 锁锚某可见
    元素, 强触发 loadEarlierHistory, 锚元素的 viewport y 位置变化必须
    < 2 px (= 内容平稳上推, 没有闪)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "load-anchor")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            for (let i = 0; i < 30; i++) {
              const d = document.createElement('div');
              d.className = 'bubble user';
              d.style.minHeight = '60px';
              d.textContent = 'existing ' + i;
              log.appendChild(d);
            }
            const firstExisting = log.firstChild;
            log.scrollTop = 0;
            const anchorBefore = firstExisting.getBoundingClientRect().top;

            state.hasMoreHistory = true;
            state.firstSeq = 100;
            state.loadingHistory = false;

            const origApi = window.api;
            window.api = async (path) => {
              if (path.includes('/messages')) {
                return {
                  messages: Array.from({length: 5}, (_, i) => ({
                    seq: 90 + i, ts: Date.now()/1000,
                    event: { type: 'user_input', content: 'earlier ' + i },
                  })),
                  first_seq: 90, has_more: false,
                };
              }
              return origApi(path);
            };
            try {
              await loadEarlierHistory();
            } finally {
              window.api = origApi;
            }

            const anchorAfter = firstExisting.getBoundingClientRect().top;
            const driftPx = Math.abs(anchorAfter - anchorBefore);
            return {
              driftPx,
              totalBubbles: log.querySelectorAll(':scope > .bubble').length,
              firstStillThere: log.contains(firstExisting),
            };
          }
        """)
        assert result["driftPx"] < 2, (
            f"anchor element drifted {result['driftPx']:.2f}px; "
            f"expected < 2 (smooth prepend, no jank)"
        )
        assert result["firstStillThere"], (
            "existing first child must remain in DOM (no detach+re-append)"
        )
        assert result["totalBubbles"] == 35, (
            f"expected 5 earlier + 30 existing = 35 bubbles, "
            f"got {result['totalBubbles']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_loadearlier_pulls_multiple_batches_to_reach_target(
    logged_in_page, base_url, test_token
):
    """运行时: 每批返回 5 条 user_input, has_more=true. 单次 loadEarlierHistory
    必须循环拉到 ≥ HISTORY_VISIBLE_TARGET (=20) 卡才停 — 这是用户的明确
    契约 "上拉到头多加载 20 条"."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "load-multi-batch")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            for (let i = 0; i < 5; i++) {
              const d = document.createElement('div');
              d.className = 'bubble user'; d.textContent = 'old ' + i;
              log.appendChild(d);
            }
            state.hasMoreHistory = true;
            state.firstSeq = 1000;
            state.loadingHistory = false;

            let calls = 0;
            const origApi = window.api;
            window.api = async (path) => {
              if (path.includes('/messages')) {
                calls += 1;
                const m = path.match(/before_seq=(\\d+)/);
                const before = m ? parseInt(m[1], 10) : 1000;
                // 每批 5 条 user_input — 4 批 × 5 = 20 命中 target
                const msgs = [];
                for (let i = 5; i >= 1; i--) {
                  msgs.push({
                    seq: before - i, ts: Date.now()/1000,
                    event: { type: 'user_input', content: 'msg ' + (before-i) },
                  });
                }
                return { messages: msgs, first_seq: before-5, has_more: true };
              }
              return origApi(path);
            };
            try { await loadEarlierHistory(); }
            finally { window.api = origApi; }

            const newCount = log.querySelectorAll(':scope > .bubble').length - 5;
            return { calls, newCount, hasMore: state.hasMoreHistory };
          }
        """)
        assert result["newCount"] >= 20, (
            f"expected ≥20 new cards (per user contract), "
            f"got {result['newCount']} after {result['calls']} calls"
        )
        assert result["hasMore"] is True
    finally:
        api_delete_session(base_url, test_token, sid)


def test_loadearlier_continues_until_top_is_not_tool_group(
    logged_in_page, base_url, test_token
):
    """运行时: 多批都是 tool_use 时, 顶端会一直是 tool-group, 必须继续拉,
    直到拉到一条非 tool_use 的消息为止 (这样 tool-group 在顶端有明确边界,
    不是被截断的)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "load-tool-complete")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            const sentinel = document.createElement('div');
            sentinel.className = 'bubble user'; sentinel.textContent = 'pivot';
            sentinel.id = 'pivot';
            log.appendChild(sentinel);

            state.hasMoreHistory = true;
            state.firstSeq = 100;
            state.loadingHistory = false;

            let calls = 0;
            const origApi = window.api;
            // 编排: 前 3 批全是 tool_use, 第 4 批最老一条 user_input
            // batches:
            //   call 1 (before=100): 4 tool_use (96-99), has_more=true
            //   call 2 (before=96): 4 tool_use (92-95), has_more=true
            //   call 3 (before=92): 4 tool_use (88-91), has_more=true
            //   call 4 (before=88): 1 user_input (87) + 3 tool_use (84-86?)
            //          → 实际只放 1 user_input (87), has_more=false
            window.api = async (path) => {
              if (path.includes('/messages')) {
                calls += 1;
                const m = path.match(/before_seq=(\\d+)/);
                const before = m ? parseInt(m[1], 10) : 100;
                if (calls <= 3) {
                  // 4 个 tool_use
                  const msgs = [];
                  for (let i = 4; i >= 1; i--) {
                    const seq = before - i;
                    msgs.push({
                      seq, ts: Date.now()/1000,
                      event: { type: 'assistant', message: { id: 'm-' + seq,
                        content: [{ type: 'tool_use', id: 'tu-' + seq,
                                    name: 'Bash', input: { command: 'echo ' + seq } }] } },
                    });
                  }
                  return { messages: msgs, first_seq: before-4, has_more: true };
                } else {
                  return {
                    messages: [{
                      seq: before-1, ts: Date.now()/1000,
                      event: { type: 'user_input', content: 'older user' },
                    }],
                    first_seq: before-1, has_more: false,
                  };
                }
              }
              return origApi(path);
            };
            try { await loadEarlierHistory(); }
            finally { window.api = origApi; }

            // 顶端 (chat-log.firstElementChild) 必须不是 tool-group
            const top = log.firstElementChild;
            return {
              calls,
              topClass: top ? top.className : null,
              topIsToolGroup: top ? top.classList.contains('tool-group') : false,
              totalCards: log.querySelectorAll(
                ':scope > .bubble, :scope > .tool-group'
              ).length,
              toolGroupCount: log.querySelectorAll(':scope > .tool-group').length,
              hasMore: state.hasMoreHistory,
            };
          }
        """)
        assert not result["topIsToolGroup"], (
            f"top of chat-log must not be a tool-group after loadEarlierHistory "
            f"(was {result['topClass']}); pulled {result['calls']} batches"
        )
        assert result["calls"] >= 4, (
            f"expected to pull all 4 batches (3 tool-only + 1 with user_input) "
            f"to break the tool-group chain, got {result['calls']}"
        )
        # 三批 4 个 tool_use 应该合成 1 个 tool-group (12 tool_use 一组)
        assert result["toolGroupCount"] == 1, (
            f"expected exactly 1 tool-group after merge, "
            f"got {result['toolGroupCount']}"
        )
        assert not result["hasMore"], (
            "last batch returned has_more=false; state.hasMoreHistory should follow"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_loadearlier_hard_caps_total_cards(
    logged_in_page, base_url, test_token
):
    """运行时: 即使 has_more=true 一直返 user_input (顶端永远非 tool-group,
    但每批就 1 条 — 模拟 raw event 密度低的最坏情况), 单次 loadEarlierHistory
    新增的可见卡必须 ≤ HARD_CAP (15). 防止用户报告的"上拉一次加载几十条"."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "load-hard-cap")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            const existing = 3;
            for (let i = 0; i < existing; i++) {
              const d = document.createElement('div');
              d.className = 'bubble user'; d.textContent = 'old ' + i;
              log.appendChild(d);
            }
            state.hasMoreHistory = true;
            state.firstSeq = 1000;
            state.loadingHistory = false;

            const origApi = window.api;
            let calls = 0;
            window.api = async (path) => {
              if (path.includes('/messages')) {
                calls += 1;
                // 每批就 1 条 user_input (模拟 server raw-event 密度低)
                const m = path.match(/before_seq=(\\d+)/);
                const before = m ? parseInt(m[1], 10) : 1000;
                return {
                  messages: [{
                    seq: before-1, ts: Date.now()/1000,
                    event: { type: 'user_input', content: 'm' + (before-1) },
                  }],
                  first_seq: before-1,
                  has_more: true,
                };
              }
              return origApi(path);
            };
            try { await loadEarlierHistory(); }
            finally { window.api = origApi; }

            const total = log.querySelectorAll(
              ':scope > .bubble, :scope > .tool-group, '
              + ':scope > .perm-card, :scope > .askuser-card'
            ).length;
            return { calls, totalCards: total, newCards: total - existing };
          }
        """)
        # 读 CSS-time 常量 (HARD_CAP) 做断言, 跟代码同步而不是写死数字
        cap_text = pathlib.Path(
            "claude_code_remote/server/static/app.js"
        ).read_text()
        cap = int(
            re.search(r"HISTORY_VISIBLE_HARD_CAP\s*=\s*(\d+)", cap_text).group(1)
        )
        assert result["newCards"] <= cap, (
            f"newCards={result['newCards']} exceeds HARD_CAP={cap}; "
            f"loaded {result['calls']} batches"
        )
        # 也别太少 — 至少够 TARGET 或循环跑了几次
        assert result["newCards"] >= 5 or result["calls"] >= 5, (
            f"barely any progress: newCards={result['newCards']}, "
            f"calls={result['calls']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_loadearlier_merges_tool_groups_into_one(
    logged_in_page, base_url, test_token
):
    """运行时: 多批 tool_use 跨批连续, 合并后应该是单个 tool-group,
    内部 tool-cards 总数 = 各批 tool_use 之和. 不能出现两个相邻 tool-group."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "load-tool-merge")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.hasMoreHistory = true;
            state.firstSeq = 100;
            state.loadingHistory = false;

            let calls = 0;
            const origApi = window.api;
            window.api = async (path) => {
              if (path.includes('/messages')) {
                calls += 1;
                const m = path.match(/before_seq=(\\d+)/);
                const before = m ? parseInt(m[1], 10) : 100;
                if (calls <= 2) {
                  // 两批 5 个 tool_use
                  const msgs = [];
                  for (let i = 5; i >= 1; i--) {
                    const seq = before - i;
                    msgs.push({
                      seq, ts: Date.now()/1000,
                      event: { type: 'assistant', message: { id: 'm-' + seq,
                        content: [{ type: 'tool_use', id: 'tu-' + seq,
                                    name: 'Bash', input: { command: 'x' } }] } },
                    });
                  }
                  return { messages: msgs, first_seq: before-5, has_more: true };
                } else {
                  // 第 3 批一条 user_input 截断
                  return {
                    messages: [{
                      seq: before-1, ts: Date.now()/1000,
                      event: { type: 'user_input', content: 'older' },
                    }],
                    first_seq: before-1, has_more: false,
                  };
                }
              }
              return origApi(path);
            };
            try { await loadEarlierHistory(); }
            finally { window.api = origApi; }

            const toolGroups = log.querySelectorAll(':scope > .tool-group');
            const toolCards = log.querySelectorAll('.tool-group .tool-card');
            return {
              toolGroupCount: toolGroups.length,
              toolCardTotal: toolCards.length,
            };
          }
        """)
        assert result["toolGroupCount"] == 1, (
            f"adjacent tool_use across batches must merge into 1 tool-group, "
            f"got {result['toolGroupCount']}"
        )
        assert result["toolCardTotal"] == 10, (
            f"expected 10 tool-cards (2 batches * 5 tool_use) merged, "
            f"got {result['toolCardTotal']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
