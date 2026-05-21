"""§4 Turn card — 跟随当前对话流的特殊卡:

- conv-status 行 (#conv-status) 永久 display:none, 数据继续维护给 turn-card 用
- 一轮开始 → 创建 .turn-card.turn-active, append chat-log 末尾, 内含:
  闪烁 model icon + ↓<token>t + <duration>
- 一轮进行 → refreshConvStatus 同步刷新文本; MutationObserver 把 card 推回末尾
  (新到 assistant / tool 消息 append 在它**之前**)
- 一轮结束 (turnEndAt 从 null → 非空) → finalize: 移除 .turn-active class
  (icon 停闪), observer 断开. 下一条新消息追加在它后面, card 自然变非末尾.
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

def test_conv_status_row_hidden():
    """CSS: #conv-status 必须 display: none."""
    css = pathlib.Path(
        "claude_code_remote/server/static/style.css"
    ).read_text()
    m = re.search(r"#conv-status\s*\{([^}]+)\}", css)
    assert m, "#conv-status rule not found"
    body = m.group(1)
    assert "display: none" in body, (
        f"#conv-status must be display:none, got: {body}"
    )


def test_turn_card_helpers_exist():
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    assert "_ensureTurnCard" in src
    assert "_finalizeTurnCard" in src
    assert "_refreshTurnCard" in src
    assert "turn-active" in src
    # applyTurnState 必须含 turn-card 生命周期调用
    m = re.search(
        r"function applyTurnState\([^)]*\)\s*\{(.*?)^\}", src, re.S | re.M
    )
    assert m, "applyTurnState body not found"
    body = m.group(1)
    assert "_ensureTurnCard" in body
    assert "_finalizeTurnCard" in body


# ---------- 运行时 ----------

def test_turn_card_created_on_turn_start(
    logged_in_page, base_url, test_token
):
    """模拟 turn 开始 → .turn-card.turn-active 出现在 chat-log 末尾."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "turn-card-create")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.turnStartAt = Date.now() - 3000;
            state.turnEndAt = null;
            state.curOutputTokens = 1234;
            state.currentMsgModel = 'claude-opus-4-7';
            applyTurnState({
              turn_started_at: state.turnStartAt / 1000,
              turn_ended_at: null,
              output_tokens: 1234,
              model: 'claude-opus-4-7',
            });
            const card = log.querySelector('.turn-card');
            return {
              hasCard: !!card,
              isActive: card && card.classList.contains('turn-active'),
              isLast: card && log.lastElementChild === card,
              tokensText: card && card.querySelector('.turn-card-tokens')
                            ?.textContent,
            };
          }
        """)
        assert result["hasCard"], "turn-card must be created on turn start"
        assert result["isActive"], "turn-card must have .turn-active class"
        assert result["isLast"], "turn-card must be last child of chat-log"
        assert "1,234" in (result["tokensText"] or ""), (
            f"tokens text should reflect curOutputTokens: {result}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_turn_card_stays_at_end_on_new_message(
    logged_in_page, base_url, test_token
):
    """新 bubble 被 append 后, MutationObserver 把 turn-card 推回末尾."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "turn-card-pin")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.turnStartAt = Date.now();
            state.turnEndAt = null;
            applyTurnState({
              turn_started_at: state.turnStartAt / 1000,
              turn_ended_at: null,
            });
            // 模拟一条新消息插入 (chat-log.appendChild)
            const newBubble = document.createElement('div');
            newBubble.className = 'bubble assistant';
            newBubble.textContent = 'after turn card';
            log.appendChild(newBubble);
            // MutationObserver 异步, 等一帧 + 微秒
            await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
            await new Promise(r => setTimeout(r, 20));
            const last = log.lastElementChild;
            return {
              lastIsTurnCard: last && last.classList.contains('turn-card'),
              bubbleStillPresent: log.contains(newBubble),
            };
          }
        """)
        assert result["lastIsTurnCard"], (
            "turn-card must be pushed back to last position after new message"
        )
        assert result["bubbleStillPresent"], (
            "the new bubble must still be in DOM (just not last)"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_classify_handles_turn_summary():
    """白盒: server _classify 必须把 _ccr/turn_summary 当成可持久化 kind,
    否则 backlog 回放时拿不到历史轮次."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/server/session_manager.py"
    ).read_text()
    m = re.search(r"def _classify\(evt[^)]*\)[^:]*:(.*?)return None", src, re.S)
    assert m, "_classify body not found"
    body = m.group(1)
    assert '"turn_summary"' in body, (
        "_classify must classify _ccr/turn_summary as a persistable kind"
    )


def test_result_triggers_turn_summary_inject():
    """白盒: _deliver 内 result event 处理后必须 inject _ccr/turn_summary."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/server/session_manager.py"
    ).read_text()
    # 找 _deliver 内 _t == "result" 段
    idx = src.find('elif _t == "result":')
    assert idx > 0
    chunk = src[idx:idx + 2000]
    assert "turn_summary" in chunk, (
        "result handling must inject _ccr/turn_summary"
    )
    assert "_deliver(sess, summary)" in chunk, (
        "summary must be persisted via _deliver pipeline"
    )


def test_turn_summary_dedup_by_turn_started_at():
    """白盒: claude CLI 同一轮可能 emit 多个 result event (interrupt / retry).
    server 必须按 sess.turn_started_at 去重, 避免 DB 落多张相同值 summary,
    回放时 chat-log 出现连续 N 张完全一样的 turn-card."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/server/session_manager.py"
    ).read_text()
    idx = src.find('elif _t == "result":')
    assert idx > 0
    chunk = src[idx:idx + 2000]
    assert "_last_summary_turn_start" in chunk, (
        "result handler must guard summary inject with a per-turn dedup key "
        "(e.g. sess._last_summary_turn_start)"
    )


def test_turn_summary_renders_finalized_card(
    logged_in_page, base_url, test_token
):
    """运行时: handleEvent 收到 _ccr/turn_summary → 渲染一张 finalized
    .turn-card 到 chat-log, 含 model icon + token + duration."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "turn-summary-replay")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            // 确保 state._turnCard 是 null (模拟 backlog replay 路径)
            state._turnCard = null;
            // 调 handleEvent (handleEvent 在 module top scope, 应可 global 调)
            handleEvent({
              type: '_ccr',
              subtype: 'turn_summary',
              turn_started_at: Date.now() / 1000 - 5,
              turn_ended_at: Date.now() / 1000,
              output_tokens: 4321,
              model: 'claude-opus-4-7-20251201',
            }, Date.now() / 1000);
            const card = log.querySelector('.turn-card');
            return {
              hasCard: !!card,
              isFinalized: card && !card.classList.contains('turn-active'),
              tokens: card && card.querySelector('.turn-card-tokens')
                          ?.textContent,
              hasIcon: card && card.querySelector('.turn-card-icon svg') != null,
            };
          }
        """)
        assert result["hasCard"], "turn_summary event must render a .turn-card"
        assert result["isFinalized"], (
            "rendered card must NOT have .turn-active class"
        )
        assert "4,321" in (result["tokens"] or ""), (
            f"tokens text should show 4,321: {result}"
        )
        assert result["hasIcon"], (
            "icon SVG should be present (model contains 'opus' → tier svg)"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_no_dup_card_when_summary_then_refresh(
    logged_in_page, base_url, test_token
):
    """覆盖 bug: 强刷进 session, backlog 回放 turn_summary 渲一张 finalized,
    然后 first_paint 推 turn_state (含 ended_at) → _refreshTurnCard 兜底
    不能再创建第二张. chat-log 应恰好 1 张 turn-card."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "no-dup")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            // 1) backlog 回放: turn_summary event 渲一张 finalized
            handleEvent({
              type: '_ccr',
              subtype: 'turn_summary',
              turn_started_at: Date.now() / 1000 - 5,
              turn_ended_at: Date.now() / 1000,
              output_tokens: 200,
              model: 'claude-sonnet-4-6',
            }, Date.now() / 1000);
            // 2) 模拟 first_paint 设 state.turnStartAt/turnEndAt + refresh
            state.turnStartAt = Date.now() - 5000;
            state.turnEndAt = Date.now();
            refreshConvStatus();
            return {
              cardCount: log.querySelectorAll('.turn-card').length,
            };
          }
        """)
        assert result["cardCount"] == 1, (
            f"only one turn-card expected (summary already rendered), "
            f"got {result['cardCount']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_ensure_adopts_existing_active_card_no_dup(
    logged_in_page, base_url, test_token
):
    """覆盖 bug: cache restore 后 chat-log 内已有 active card, state._turnCard
    ref 清空. 重复 ensure 必须接管现有 card, 不能 append 新的. 否则反复进入
    session 会堆一堆 active card."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "ensure-no-dup")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            // 模拟一张已有的 active card (cache restore 还原), state ref 清空
            const old = document.createElement('div');
            old.className = 'turn-card turn-active';
            old.innerHTML = '<span class="turn-card-icon"></span>'
              + '<span class="turn-card-tokens">↓0t</span>'
              + '<span class="turn-card-time">0s</span>';
            log.appendChild(old);
            state._turnCard = null;
            if (state._turnCardObserver) {
              state._turnCardObserver.disconnect();
              state._turnCardObserver = null;
            }
            // 进 session 模拟多次 ensure (refreshConvStatus 兜底)
            _ensureTurnCard();
            _ensureTurnCard();
            _ensureTurnCard();
            return {
              activeCount: log.querySelectorAll('.turn-card.turn-active').length,
              adoptedOld: state._turnCard === old,
            };
          }
        """)
        assert result["activeCount"] == 1, (
            f"only 1 active turn-card after multiple ensure calls, "
            f"got {result['activeCount']}"
        )
        assert result["adoptedOld"], (
            "ensure must adopt the existing active card, not create a new one"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_handle_user_input_during_earlier_fragment_skips_ensure(
    logged_in_page, base_url, test_token
):
    """覆盖 bug: 强刷进 idle session → backlog 回放渲一张 finalized turn_summary
    → autoFillInitialCards 跑 loadEarlierHistory → 历史 user_input event
    渲到 earlierFragment. handleUserInput 看 isHistoryReplay=false (backlog_done
    已设), 但 state.earlierFragment 非空 — 仍属于历史回放, 不能误建 active
    turn-card. Guard: !state.isHistoryReplay && !state.earlierFragment 才 ensure."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "earlier-no-dup")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            state.isHistoryReplay = false;
            // 1) 模拟 backlog_done 后已渲一张 finalized turn-card
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: Date.now()/1000 - 5,
              turn_ended_at: Date.now()/1000,
              output_tokens: 50, model: 'claude-sonnet-4-6',
            }, Date.now()/1000);
            const beforeCount = log.querySelectorAll('.turn-card').length;
            // 2) 模拟 loadEarlierHistory 路径: earlierFragment 设上, 然后
            //    handleUserInput 喂一个历史 user_input.
            state.earlierFragment = document.createDocumentFragment();
            handleEvent({ type: 'user_input', content: 'older user' },
                        Date.now()/1000 - 100);
            state.earlierFragment = null;
            const afterCount = log.querySelectorAll('.turn-card').length;
            const activeCount = log.querySelectorAll(
              '.turn-card.turn-active'
            ).length;
            return { beforeCount, afterCount, activeCount };
          }
        """)
        assert result["beforeCount"] == 1
        assert result["afterCount"] == 1, (
            f"loadEarlierHistory 的历史 user_input 不能新建 turn-card. "
            f"got {result['afterCount']} cards"
        )
        assert result["activeCount"] == 0, (
            f"absolutely no .turn-active card after replaying historical "
            f"user_input: got {result['activeCount']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_turn_summary_skipped_when_active_card_just_finalized(
    logged_in_page, base_url, test_token
):
    """实时路径: applyTurnState 先 finalize active card, 然后 turn_summary
    到达 — _renderTurnSummary 必须 skip (state._turnCard 已存且 non-active),
    不渲重复卡."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "turn-summary-skip")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            // 1) 开启 turn
            state.turnStartAt = Date.now() - 5000;
            state.turnEndAt = null;
            applyTurnState({
              turn_started_at: state.turnStartAt / 1000,
              turn_ended_at: null,
            });
            // 2) 结束 turn (active → finalized)
            applyTurnState({
              turn_started_at: state.turnStartAt / 1000,
              turn_ended_at: Date.now() / 1000,
            });
            // 3) turn_summary 紧随其后到达
            handleEvent({
              type: '_ccr',
              subtype: 'turn_summary',
              turn_started_at: state.turnStartAt / 1000,
              turn_ended_at: Date.now() / 1000,
              output_tokens: 100,
              model: 'claude-sonnet-4-6',
            }, Date.now() / 1000);
            return {
              cardCount: log.querySelectorAll('.turn-card').length,
            };
          }
        """)
        assert result["cardCount"] == 1, (
            f"only the just-finalized active card should remain (no dup from "
            f"turn_summary), got {result['cardCount']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_turn_card_idempotent_summary_before_state(
    logged_in_page, base_url, test_token
):
    """时序对抗 1: turn_summary 先到, turn_state(end_at 非空) 后到. 期望:
    chat-log 最终只 1 张 turn-card (按 data-turn-start 幂等去重)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tc-summary-first")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            state.isHistoryReplay = false;
            state.turnStartAt = null;
            state.turnEndAt = null;
            const ts = Date.now() / 1000 - 5;
            const te = Date.now() / 1000;
            // 1) turn_summary 抢先到 (server 出来时序乱)
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: ts, turn_ended_at: te,
              output_tokens: 999, model: 'claude-opus-4-7',
            }, te);
            // 2) turn_state (含 ended_at) 后到, 触发实时 lifecycle
            applyTurnState({
              turn_started_at: ts, turn_ended_at: te,
            });
            return {
              cardCount: log.querySelectorAll('.turn-card').length,
              activeCount: log.querySelectorAll(
                '.turn-card.turn-active'
              ).length,
            };
          }
        """)
        assert result["cardCount"] == 1, (
            f"summary-first 时序仍应只 1 张, got {result['cardCount']}"
        )
        assert result["activeCount"] == 0, (
            "已结束 turn 不应有 active card"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_turn_card_idempotent_state_then_summary(
    logged_in_page, base_url, test_token
):
    """时序对抗 2: 实时 lifecycle 先建 active → finalize, 然后 turn_summary
    到 (server 也 inject 了). chat-log 最终只 1 张."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tc-state-first")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            state.isHistoryReplay = false;
            const ts = Date.now() / 1000 - 5;
            const te = Date.now() / 1000;
            // 1) 实时 turn start
            state.turnStartAt = null;
            state.turnEndAt = null;
            applyTurnState({ turn_started_at: ts, turn_ended_at: null });
            // 2) 实时 turn end
            applyTurnState({ turn_started_at: ts, turn_ended_at: te });
            // 3) turn_summary 也到了
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: ts, turn_ended_at: te,
              output_tokens: 250, model: 'claude-sonnet-4-6',
            }, te);
            return {
              cardCount: log.querySelectorAll('.turn-card').length,
              activeCount: log.querySelectorAll(
                '.turn-card.turn-active'
              ).length,
              tokens: log.querySelector('.turn-card .turn-card-tokens')
                        ?.textContent || '',
            };
          }
        """)
        assert result["cardCount"] == 1, (
            f"state-first 时序仍应只 1 张, got {result['cardCount']}"
        )
        assert result["activeCount"] == 0, "已结束不应有 active"
        # summary 数据应被采用 (实时 active 没有 tokens 数据)
        assert "250" in result["tokens"], (
            f"summary 的 tokens 应被写进唯一 card: got {result['tokens']!r}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_turn_card_idempotent_duplicate_summary(
    logged_in_page, base_url, test_token
):
    """时序对抗 3: 同 turn 多次 turn_summary 到达 (e.g. server dedup 漏了).
    chat-log 最终只 1 张, 不堆积."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tc-dup-summary")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            const ts = Date.now() / 1000 - 5;
            const te = Date.now() / 1000;
            for (let i = 0; i < 5; i++) {
              handleEvent({
                type: '_ccr', subtype: 'turn_summary',
                turn_started_at: ts, turn_ended_at: te,
                output_tokens: 100, model: 'claude-opus-4-7',
              }, te);
            }
            return {
              cardCount: log.querySelectorAll('.turn-card').length,
            };
          }
        """)
        assert result["cardCount"] == 1, (
            f"5 次 dup summary 仍应只 1 张, got {result['cardCount']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_turn_card_distinct_turns_keep_separate_cards(
    logged_in_page, base_url, test_token
):
    """正向: 不同 turn_started_at 的 summary 各自渲一张, 不互相覆盖."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tc-distinct")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state._turnCard = null;
            const now = Date.now() / 1000;
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: now - 100, turn_ended_at: now - 95,
              output_tokens: 50, model: 'claude-sonnet-4-6',
            }, now - 95);
            handleEvent({
              type: '_ccr', subtype: 'turn_summary',
              turn_started_at: now - 50, turn_ended_at: now - 45,
              output_tokens: 60, model: 'claude-opus-4-7',
            }, now - 45);
            return {
              cardCount: log.querySelectorAll('.turn-card').length,
            };
          }
        """)
        assert result["cardCount"] == 2, (
            f"不同 turnStart 的 summary 应保留各自 card, got {result['cardCount']}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_tool_group_merges_with_active_turn_card_at_tail(
    logged_in_page, base_url, test_token
):
    """覆盖 bug: turn 进行中, active turn-card 被 MutationObserver 粘在 chat-log
    末尾. 连续到来的 tool_use 必须合并到同一个 .tool-group, 而不是因为
    "末尾不是 tool-group" 就各起新 group. 用户在后台跑任务看到的现象是
    一堆 tool-card 不合并."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "tool-group-merge")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.currentToolGroup = null;
            state.toolById = new Map();
            // 1) 开 turn: active turn-card append 到末尾 + observer 启动
            state.turnStartAt = Date.now();
            state.turnEndAt = null;
            applyTurnState({
              turn_started_at: state.turnStartAt / 1000,
              turn_ended_at: null,
            });
            // 2) 模拟两次连续 tool_use
            ensureToolCard('tool-1', 'Read');
            // 等 observer 把 turn-card 推回末尾
            await new Promise(r => requestAnimationFrame(
              () => requestAnimationFrame(r)
            ));
            await new Promise(r => setTimeout(r, 20));
            ensureToolCard('tool-2', 'Bash');
            await new Promise(r => requestAnimationFrame(
              () => requestAnimationFrame(r)
            ));
            await new Promise(r => setTimeout(r, 20));
            const groups = log.querySelectorAll('.tool-group');
            const cardsInFirst = groups[0]
              ? groups[0].querySelectorAll('.tool-card').length : 0;
            return {
              groupCount: groups.length,
              cardsInFirst,
              lastIsTurnCard: log.lastElementChild
                              && log.lastElementChild.classList.contains('turn-card'),
            };
          }
        """)
        assert result["groupCount"] == 1, (
            f"两个连续 tool_use 必须合并到 1 个 tool-group, "
            f"got {result['groupCount']}"
        )
        assert result["cardsInFirst"] == 2, (
            f"两张 tool-card 必须都在第一个 group 内, got {result['cardsInFirst']}"
        )
        assert result["lastIsTurnCard"], (
            "turn-card 还是该粘在末尾"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_turn_card_finalize_on_turn_end(
    logged_in_page, base_url, test_token
):
    """turn 结束 (turnEndAt 边沿) → 移除 .turn-active, observer 断, 下一条
    新消息append 在 turn-card 之后 (它不再粘末尾)."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "turn-card-finalize")
    try:
        _enter_chat(page, sid)
        result = page.evaluate("""
          async () => {
            const log = document.getElementById('chat-log');
            log.innerHTML = '';
            state.turnStartAt = Date.now() - 5000;
            state.turnEndAt = null;
            applyTurnState({
              turn_started_at: state.turnStartAt / 1000,
              turn_ended_at: null,
            });
            const card = log.querySelector('.turn-card');
            // 结束 turn — 不能提前手动改 state.turnEndAt, 让 applyTurnState
            // 自己捕获 prevEndAt (此时仍为 null) → 检测到边沿 → finalize.
            applyTurnState({
              turn_started_at: state.turnStartAt / 1000,
              turn_ended_at: Date.now() / 1000,
            });
            const stillActive = card.classList.contains('turn-active');
            // 模拟下一条 new message append — turn-card 不应再被推回末尾
            const after = document.createElement('div');
            after.className = 'bubble user';
            after.textContent = 'after';
            log.appendChild(after);
            await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
            await new Promise(r => setTimeout(r, 20));
            const last = log.lastElementChild;
            return {
              stillActive,
              lastIsBubble: last && last !== card,
              cardClasses: card.className,
            };
          }
        """)
        assert not result["stillActive"], (
            f".turn-active must be removed after finalize: {result['cardClasses']}"
        )
        assert result["lastIsBubble"], (
            "finalized turn-card must NOT be pushed back; new bubble stays last"
        )
    finally:
        api_delete_session(base_url, test_token, sid)
