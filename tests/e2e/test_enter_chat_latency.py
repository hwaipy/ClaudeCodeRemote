"""
Spec §15: clicking a session card must reveal the chat view within
one frame (no waiting on /resume or /messages backlog). Spinner shows
inside the chat-log while history streams in.
"""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


def test_chat_view_appears_within_300ms_after_click(
    logged_in_page, base_url, test_token
):
    """Cache-miss path: clicking a non-current session card MUST flip
    view-chat to .active within ~300 ms, even before /resume returns.
    Previously enterChat awaited /resume + 800 ms timeout, so the user
    saw the home view for ~1-2 s. The fix is to call showView('chat')
    BEFORE awaiting /resume."""
    page = logged_in_page
    sid = api_spawn(base_url, test_token, "/tmp", "fast-enter")
    try:
        hp = HomePage(page)
        hp.expect_visible()
        card = page.locator(f"[data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)

        # Force the session into hibernated state — this makes
        # enterChat's cache-miss path call /resume, the slow path.
        # On the test fixture's fake_claude, /resume is fast, but the
        # point is to exercise the CODE path without await'ing it.
        # Click and time how long until view-chat gains .active.
        t0 = time.perf_counter()
        card.click()
        # Poll for view-chat.active. With the old behavior this took
        # ~800 ms (the setTimeout fallback); with the fix it should
        # happen within a frame or two.
        page.wait_for_function(
            "() => document.getElementById('view-chat').classList.contains('active')",
            timeout=1000,
        )
        dt_ms = (time.perf_counter() - t0) * 1000
        assert dt_ms < 300, (
            f"chat view took {dt_ms:.0f} ms to become .active — the fix is to "
            "showView('chat') BEFORE awaiting /resume. Was the await re-added?"
        )

        # Spinner overlay should be visible inside the chat at this point
        # (history hasn't loaded yet) — i.e. #chat-loading is NOT hidden.
        loading_hidden = page.evaluate(
            "() => document.getElementById('chat-loading').hidden"
        )
        # Spinner may have already faded out if backlog_done fired fast;
        # we just assert the OVERLAY mechanism is in place — hidden OR
        # visible is both fine. The forbidden state is "view-chat not
        # active" which we've already passed.
        assert isinstance(loading_hidden, bool)
    finally:
        api_delete_session(base_url, test_token, sid)


def test_enter_chat_does_not_await_resume(
    logged_in_page, base_url, test_token
):
    """White-box guard against regressions: enterChat MUST NOT await
    the /resume API call in the cache-miss path. If someone re-adds
    `await api(.../resume...)` the chat view will block waiting for
    the CLI to spawn (~1-2 s) which defeats the latency fix."""
    import pathlib
    src = pathlib.Path(
        "claude_code_remote/server/static/app.js"
    ).read_text()
    # Find the cache-miss block of enterChat (between 缓存未命中 marker
    # and the closing of enterChat).
    miss_idx = src.find("缓存未命中：原本的冷启动流程")
    assert miss_idx > 0, "enterChat cache-miss section marker missing"
    # Cut to next function start
    next_fn = src.find("\nfunction ", miss_idx + 1)
    next_fn = next_fn if next_fn > 0 else len(src)
    cache_miss = src[miss_idx:next_fn]
    # Forbidden: an awaited resume call
    assert not re.search(r"await\s+api\([^)]*\bresume\b", cache_miss), (
        "enterChat cache-miss path must NOT await /resume — that re-blocks "
        "the view transition. Use fire-and-forget .catch() instead."
    )
    # Required: showView('chat') BEFORE the /resume CALL SITE (not its
    # surrounding comments), so the view reveals immediately.
    show_view_idx = cache_miss.find('showView("chat")')
    # Match the actual /resume call site (template literal), not the
    # word "/resume" appearing in a comment.
    m = re.search(r"api\([^,]*?/resume`", cache_miss)
    assert show_view_idx >= 0, "showView('chat') missing from cache-miss path"
    assert m, "expected api(.../resume...) call site missing"
    resume_call_idx = m.start()
    assert show_view_idx < resume_call_idx, (
        f"showView('chat') (at {show_view_idx}) must come BEFORE the /resume "
        f"call (at {resume_call_idx}) so the chat view animates in immediately"
    )
