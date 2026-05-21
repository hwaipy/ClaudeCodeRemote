"""Token 显示格式 (用户最终意图):

- **不写 "tokens"/"tok" 字样**
- **数字千分位逗号分隔** (1234 → "1,234")
- **末尾紧跟 "t"** (无空格)
- **数字前加 "↓" 箭头** (output token / msg meta), 无空格

格式化函数 _fmtTok(n) 返回 "千分位 + t". 调用点拼 "↓" 前缀.

适用位置:
- conv-status 的 #cs-tokens: "↓1,234t"
- assistant 气泡 meta 的 .msg-tokens: "↓1,234t"
- ctx tooltip detail: "1,234t / 200,000t" (无箭头, 它是 ctx 用量不是 output)
"""
from __future__ import annotations

import re
import pathlib


def test_fmttok_unit_via_page(logged_in_page):
    """单元: _fmtTok 返回 "千分位 + t". 0 也带 t."""
    page = logged_in_page
    out = page.evaluate("""
      () => ({
        zero: _fmtTok(0),
        small: _fmtTok(999),
        thousand: _fmtTok(1000),
        oneK_two: _fmtTok(1234),
        ten_k: _fmtTok(12345),
        hundred_k: _fmtTok(123456),
        million: _fmtTok(1234567),
      })
    """)
    assert out["zero"] == "0t", out
    assert out["small"] == "999t", out
    assert out["thousand"] == "1,000t", out
    assert out["oneK_two"] == "1,234t", out
    assert out["ten_k"] == "12,345t", out
    assert out["hundred_k"] == "123,456t", out
    assert out["million"] == "1,234,567t", out


def test_fmttok_no_k_or_m_suffix():
    """白盒: _fmtTok 实现里不能再有 k/M 简写 — 用户要看具体数字千分位."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(r"function _fmtTok\(n\)\s*\{(.*?)\n\}", src, re.S)
    assert m, "_fmtTok function not found"
    body = m.group(1)
    assert '"k"' not in body and '" k"' not in body, (
        f"_fmtTok must not produce k abbreviation: {body}"
    )
    assert '"M"' not in body and '" M"' not in body, (
        f"_fmtTok must not produce M abbreviation: {body}"
    )
    # 应该用 toLocaleString 做千分位
    assert "toLocaleString" in body, (
        f"_fmtTok should use toLocaleString for thousands separator: {body}"
    )


def test_cs_tokens_uses_arrow_and_fmttok():
    """白盒: #cs-tokens 文本必须是 "↓" + _fmtTok(...), 不能含 "tokens"/"token"
    字样, 不能在 ↓ 跟数字之间留空格."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(
        r"tokens\.textContent\s*=\s*([`'\"][^`'\";]+[`'\"])",
        src,
    )
    assert m, "tokens.textContent assignment not found"
    decl = m.group(1)
    assert "_fmtTok" in decl, (
        f"cs-tokens must format via _fmtTok: {decl}"
    )
    assert "↓" in decl, f"cs-tokens must prefix with ↓: {decl}"
    # 禁止 "tokens" / " token" 字样
    assert "tokens" not in decl and "token" not in decl, (
        f"cs-tokens must not contain 'token(s)' word: {decl}"
    )
    # 箭头跟数字之间不留空格 — 即 "↓${_fmtTok(...)}" 而不是 "↓ ${_fmtTok(...)}"
    assert "↓ " not in decl, (
        f"no space between ↓ and number in cs-tokens: {decl}"
    )


def test_assistant_meta_tokens_uses_arrow_and_fmttok():
    """白盒: assistant 气泡 meta 的 token 显示 = "↓" + _fmtTok(...), 不带
    " tok" 字样, ↓ 跟数字间无空格."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    # 找设置 msg-tokens 的整行
    idx = src.find('querySelector(".msg-tokens")')
    assert idx > 0, "msg-tokens setter not found"
    # 后面 200 char 应该包含 textContent 赋值
    chunk = src[idx:idx + 300]
    m = re.search(r"textContent\s*=\s*([^;]+);", chunk)
    assert m, f"msg-tokens textContent assignment not found in: {chunk}"
    expr = m.group(1)
    assert "_fmtTok" in expr, (
        f"msg-tokens must format via _fmtTok, got: {expr}"
    )
    assert "↓" in expr, f"msg-tokens must prefix with ↓: {expr}"
    assert " tok" not in expr, (
        f"msg-tokens must not include ' tok' suffix anymore: {expr}"
    )
    assert "↓ " not in expr, (
        f"no space between ↓ and _fmtTok in msg-tokens: {expr}"
    )


def test_ctx_detail_uses_fmtctx_kM():
    """白盒: ctx 详细文本用 _fmtCtx (k/M 简写), 不是 _fmtTok (千分位+t).
    Ctx 切回 SVG 圆环 + #ctx-tooltip 模式 (跟之前一模一样), tooltip 内
    #ctx-tooltip-detail 文本走 _fmtCtx. Output token (cs-tokens / msg-tokens)
    仍走 _fmtTok 千分位."""
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    m = re.search(
        r"tipDetail\.textContent\s*=\s*([`'\"][^`'\"]+[`'\"])",
        src,
    )
    assert m, "tipDetail.textContent assignment not found"
    decl = m.group(1)
    assert "_fmtCtx" in decl, (
        f"ctx tooltip detail must format via _fmtCtx (k/M abbrev): {decl}"
    )
    assert "_fmtTok" not in decl, (
        f"ctx tooltip detail must NOT use _fmtTok (千分位+t belongs to output): {decl}"
    )
    assert "tokens" not in decl and " token" not in decl, decl


def test_fmtctx_unit_via_page(logged_in_page):
    """单元: _fmtCtx 用 k/M 简写, 数字和单位之间无空格."""
    page = logged_in_page
    out = page.evaluate("""
      () => ({
        zero: _fmtCtx(0),
        small: _fmtCtx(999),
        thousand: _fmtCtx(1000),
        twoK: _fmtCtx(2345),
        ctx200k: _fmtCtx(200000),
        million: _fmtCtx(1000000),
        bigM: _fmtCtx(1234567),
      })
    """)
    assert out["zero"] == "0", out
    assert out["small"] == "999", out
    assert out["thousand"] == "1.0k", out
    assert out["twoK"] == "2.3k", out
    assert out["ctx200k"] == "200.0k", out
    assert out["million"] == "1.0M", out
    assert out["bigM"] == "1.2M", out
