"""Spec §2: the new-session modal has a 4-button permission-mode picker.
Picking one and starting must (1) flip the server's permission_mode for
the new session and (2) write localStorage.ccr.spawnPermMode for next time.
"""
from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_list_sessions
from tests.pages.home_page import HomePage

ACTIVE_CLASS = re.compile(r"\bactive\b")


@pytest.fixture
def cleanup_test_sessions(server_env):
    yield
    try:
        for s in api_list_sessions(server_env["base_url"], server_env["token"]):
            if (s.get("name") or "").startswith("test-perm-"):
                try:
                    api_delete_session(
                        server_env["base_url"], server_env["token"], s["id"]
                    )
                except Exception:
                    pass
    except Exception:
        pass


def _get_mode(base_url: str, token: str, sid: str) -> str:
    r = httpx.get(
        f"{base_url}/api/sessions/{sid}/permission_mode",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["mode"]


def test_perm_picker_present_with_four_modes(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.open_new_modal()
    picker = logged_in_page.locator("#spawn-perm")
    expect(picker).to_be_visible()
    btns = picker.locator(".spawn-perm-btn")
    expect(btns).to_have_count(4)
    # All four modes are wired up
    for mode in ("manual", "accept_edits", "plan", "allow_all"):
        expect(picker.locator(f".spawn-perm-btn[data-mode='{mode}']")).to_be_visible()
    # Default is manual = active
    expect(
        picker.locator(".spawn-perm-btn.active")
    ).to_have_attribute("data-mode", "manual")


def test_perm_click_toggles_active(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.open_new_modal()
    picker = logged_in_page.locator("#spawn-perm")
    picker.locator(".spawn-perm-btn[data-mode='plan']").click()
    expect(picker.locator(".spawn-perm-btn.active")).to_have_count(1)
    expect(
        picker.locator(".spawn-perm-btn.active")
    ).to_have_attribute("data-mode", "plan")
    # Click another mode → previous loses .active
    picker.locator(".spawn-perm-btn[data-mode='allow_all']").click()
    expect(picker.locator(".spawn-perm-btn.active")).to_have_count(1)
    expect(
        picker.locator(".spawn-perm-btn.active")
    ).to_have_attribute("data-mode", "allow_all")


def test_perm_choice_propagates_to_server(
    logged_in_page, tmp_path, server_env, cleanup_test_sessions
):
    """Pick allow_all → start session → server reports mode=allow_all."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.fill_spawn_form(cwd=str(tmp_path), name="test-perm-allow")
    logged_in_page.locator(
        "#spawn-perm .spawn-perm-btn[data-mode='allow_all']"
    ).click()
    hp.submit_spawn()
    # Wait until chat view is active so we know the session was created
    expect(logged_in_page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    # Find the new session and ask the server for its permission mode
    sessions = api_list_sessions(server_env["base_url"], server_env["token"])
    sid = next(s["id"] for s in sessions if s.get("name") == "test-perm-allow")
    mode = _get_mode(server_env["base_url"], server_env["token"], sid)
    assert mode == "allow_all", f"expected allow_all, got {mode!r}"


def test_perm_pick_is_transient_does_not_leak_to_default(
    logged_in_page, tmp_path, server_env, cleanup_test_sessions
):
    """A per-spawn picker pick is TRANSIENT — it does NOT become the new
    default for the next session. The default is owned by the settings
    view (#view-settings) via ccr.defaultPermMode. This guards against
    accidentally treating one-off picks as preference changes."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.fill_spawn_form(cwd=str(tmp_path), name="test-perm-persist")
    logged_in_page.locator(
        "#spawn-perm .spawn-perm-btn[data-mode='accept_edits']"
    ).click()
    hp.submit_spawn()
    expect(logged_in_page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    # The spawn must NOT write ccr.defaultPermMode (settings is the source
    # of truth for defaults).
    default_after = logged_in_page.evaluate(
        '() => localStorage.getItem("ccr.defaultPermMode")'
    )
    assert default_after in (None, "manual"), (
        f"per-spawn pick must NOT change ccr.defaultPermMode, got {default_after!r}"
    )

    # Go back and re-open modal → modal resets to the default (manual),
    # not to the previous accept_edits pick.
    logged_in_page.locator("#chat-back").dispatch_event("click")
    hp.expect_visible()
    hp.open_new_modal()
    expect(
        logged_in_page.locator("#spawn-perm .spawn-perm-btn.active")
    ).to_have_attribute("data-mode", "manual")


def test_default_manual_does_not_call_set_mode(
    logged_in_page, tmp_path, server_env, cleanup_test_sessions
):
    """Default manual should still report manual after spawn (no gateway call,
    but the manager initialises it to manual anyway)."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.fill_spawn_form(cwd=str(tmp_path), name="test-perm-default")
    # Don't touch the picker — manual is the default.
    hp.submit_spawn()
    expect(logged_in_page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    sessions = api_list_sessions(server_env["base_url"], server_env["token"])
    sid = next(s["id"] for s in sessions if s.get("name") == "test-perm-default")
    mode = _get_mode(server_env["base_url"], server_env["token"], sid)
    assert mode == "manual", f"expected manual, got {mode!r}"
