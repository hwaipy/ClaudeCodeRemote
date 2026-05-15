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


def test_la_bump_kinds_contract():
    """Regression guard: the activity-bump kind set must contain real
    user / assistant message types AND user-resolution events, but must
    NOT contain CLI-initiated events. Future edits to this set should be
    explicit decisions."""
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
    # CLI-initiated events MUST NOT bump (the whole point of the filter).
    for kind in ("system_init", "perm_req", "askuser_req"):
        assert kind not in _LA_BUMP_KINDS, (
            f"{kind} must NOT be in _LA_BUMP_KINDS — CLI initiated, not user"
        )
