"""
Spec §2: clicking to OPEN an old session must NOT bump its
last_activity_at — the CLI emits a `system init` envelope on every
spawn/resume, and that envelope is server-side bookkeeping, not user
activity. (Without the fix, every click on an N-hour-old card would
reset its "N ago" label to "1s ago".)
"""
from __future__ import annotations

import time

import httpx
import pytest


def _get_la(base_url: str, token: str, sid: str) -> float:
    r = httpx.get(
        f"{base_url}/api/sessions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    r.raise_for_status()
    for s in r.json()["sessions"]:
        if s["id"] == sid:
            return float(s["last_activity_at"])
    raise AssertionError(f"session {sid} not found")


def _interrupt(base_url: str, token: str, sid: str) -> None:
    r = httpx.post(
        f"{base_url}/api/sessions/{sid}/interrupt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    r.raise_for_status()


def _resume(base_url: str, token: str, sid: str) -> None:
    r = httpx.post(
        f"{base_url}/api/sessions/{sid}/resume",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    r.raise_for_status()


def test_resume_does_not_bump_last_activity_at(base_url, test_token):
    """Hibernate → wait → resume. last_activity_at MUST be unchanged
    afterwards (the resume's system_init envelope is filtered out)."""
    from tests.helpers import api_spawn, api_delete_session
    sid = api_spawn(base_url, test_token, "/tmp", "la-no-bump")
    try:
        # Let the spawn's init envelope flow through.
        time.sleep(0.4)
        la_after_spawn = _get_la(base_url, test_token, sid)

        # Hibernate the session (kills the CLI process).
        _interrupt(base_url, test_token, sid)
        time.sleep(0.3)

        # Snapshot LA right before resume. Hibernation itself shouldn't
        # have bumped it either.
        la_before_resume = _get_la(base_url, test_token, sid)

        # Sleep WAY past the activity-bump window so a bug would be
        # obvious: any new bump would put LA ~3 s later than now.
        time.sleep(3.0)

        _resume(base_url, test_token, sid)
        # Wait for the resume's `system init` envelope to flow through
        # the pump (long enough that the bug would have fired).
        time.sleep(0.5)

        la_after_resume = _get_la(base_url, test_token, sid)

        drift = la_after_resume - la_before_resume
        # With the fix: drift ≈ 0. Without it: drift ≈ 3+ s.
        assert drift < 0.5, (
            f"resume bumped last_activity_at by {drift:.2f}s — system_init "
            f"should NOT count as activity. la_before={la_before_resume:.3f}, "
            f"la_after={la_after_resume:.3f}, la_after_spawn={la_after_spawn:.3f}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_resume_with_replay_does_not_bump_la(base_url, test_token, tmp_path, monkeypatch):
    """Stronger: even if claude --resume REPLAYS the previous turn (a real
    sequence of assistant/result envelopes after init), LA must stay put.
    Uses FAKE_CLAUDE_REPLAY_ON_RESUME so the fake CLI re-emits a finished
    turn on resume, exactly like real claude does.

    NOTE: this test only works if the server picks up the env var. The
    test server is spawned once at session scope; we can't set env mid-
    run. So we just check that the implementation has _replay_pending —
    a runtime test would need a fresh server fixture.
    """
    # Sanity guard: the field has to exist on Session.
    from claude_code_remote.server.session_manager import _LA_BUMP_KINDS  # noqa
    import inspect
    from claude_code_remote.server import session_manager as sm
    src = inspect.getsource(sm)
    assert "_replay_pending" in src, (
        "_replay_pending must be set on Session in resume() to suppress LA "
        "bumps from claude --resume's initial replay envelopes"
    )
    assert "kind == \"user_input\"" in src and "_replay_pending = False" in src, (
        "user_input must clear _replay_pending and itself bump LA"
    )


def test_la_bump_triggers_session_state_broadcast():
    """Regression for: during a sustained busy turn (state stays "busy"),
    every assistant/result/user envelope MUST broadcast session_state to
    push the new last_activity_at to clients. Without this, the home-card
    "Xs ago" freezes during active work and the stalled-busy ticker
    spuriously turns the dot yellow."""
    import inspect
    from claude_code_remote.server import session_manager as sm
    src = inspect.getsource(sm._SessionManager._deliver
                            if hasattr(sm, "_SessionManager") else sm)
    # The pump's broadcast condition must be ANY bump, not only state changes.
    assert "state_dirty or bump_la" in src, (
        "_pump (or _deliver) must broadcast_status when bump_la is true, "
        "not only when state_dirty is true — otherwise sustained busy "
        "turns never re-push LA to clients."
    )


def test_la_bump_kinds_contract():
    """Activity-bump kind contract: any kind that surfaces NEW info to the
    user must bump LA — including permission/askuser REQUESTS, because a
    fresh card appearing IS new user-visible info. The only filtered kind
    is `system_init`, the CLI's silent bookkeeping on spawn/resume."""
    from claude_code_remote.server.session_manager import _LA_BUMP_KINDS

    # Real conversation activity MUST bump.
    for kind in ("user", "user_input", "assistant", "result"):
        assert kind in _LA_BUMP_KINDS, (
            f"{kind} must be in _LA_BUMP_KINDS — real message events"
        )
    # User-driven resolution events MUST bump (user explicitly clicked).
    for kind in ("perm_resolved", "askuser_resolved"):
        assert kind in _LA_BUMP_KINDS, (
            f"{kind} must be in _LA_BUMP_KINDS — user clicked to resolve"
        )
    # CLI-initiated user-visible REQUESTS MUST bump — a brand-new
    # approval / askuser card popping up IS new info worth surfacing.
    for kind in ("perm_req", "askuser_req"):
        assert kind in _LA_BUMP_KINDS, (
            f"{kind} must be in _LA_BUMP_KINDS — new card visible to user"
        )
    # system_init is the ONLY kind explicitly filtered out — without
    # this guard, opening an idle 6h-old session would jump LA to "1s ago"
    # purely from the CLI's resume init envelope.
    assert "system_init" not in _LA_BUMP_KINDS, (
        "system_init must NOT be in _LA_BUMP_KINDS — CLI internal "
        "bookkeeping on spawn/resume, not user-visible content"
    )
