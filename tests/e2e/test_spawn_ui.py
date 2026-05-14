"""L3 spawn flow via the UI form (not the API helper)."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.helpers import api_list_sessions, api_delete_session
from tests.pages.home_page import HomePage

ACTIVE_CLASS = re.compile(r"\bactive\b")


@pytest.fixture
def cleanup_test_sessions(server_env):
    """After test, delete any session whose name starts with 'test-'."""
    yield
    try:
        for s in api_list_sessions(server_env["base_url"], server_env["token"]):
            if (s.get("name") or "").startswith("test-"):
                try:
                    api_delete_session(
                        server_env["base_url"], server_env["token"], s["id"]
                    )
                except Exception:
                    pass
    except Exception:
        pass


def test_empty_cwd_shows_error(logged_in_page):
    """cwd input defaults to '~/codes' on first paint, so the test has to
    explicitly clear it before triggering submit."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.spawn_cwd.fill("")
    hp.spawn_name.fill("would-fail")
    hp.submit_spawn()
    expect(hp.spawn_err).to_be_visible()
    expect(hp.spawn_err).to_contain_text("Working directory required")
    expect(logged_in_page.locator("#view-home")).to_have_class(ACTIVE_CLASS)


def test_chip_click_fills_cwd(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    chips = logged_in_page.locator("#cwd-presets .chip")
    expect(chips.first).to_be_visible()

    # Click the "codes" chip — fills cwd with ~/codes
    codes_chip = logged_in_page.locator("#cwd-presets .chip", has_text="codes").first
    codes_chip.click()
    expect(hp.spawn_cwd).to_have_value("~/codes")


def test_chip_marked_active_when_cwd_matches(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    # Type a path that matches a preset → that chip gains .active
    hp.spawn_cwd.fill("~/codes")
    expect(
        logged_in_page.locator("#cwd-presets .chip.active", has_text="codes")
    ).to_be_visible()


def test_spawn_ui_flow_enters_chat(logged_in_page, tmp_path, cleanup_test_sessions):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.fill_spawn_form(cwd=str(tmp_path), name="test-ui-spawn")
    hp.submit_spawn()

    # body.has-session signals we entered chat
    expect(logged_in_page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
    expect(logged_in_page.locator("#view-chat")).to_have_class(ACTIVE_CLASS)
    expect(logged_in_page.locator("#chat-name")).to_have_text("test-ui-spawn")
    # name input cleared after success
    expect(hp.spawn_name).to_have_value("")


def test_starting_button_briefly_disables(logged_in_page, tmp_path,
                                          cleanup_test_sessions):
    """After click, button disables and label changes to 'Starting…' until
    server responds. We catch it via the disabled state."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    hp.fill_spawn_form(cwd=str(tmp_path), name="test-disable")

    # Slow the network so we can observe the in-flight state
    logged_in_page.route("**/api/spawn", lambda route: (
        logged_in_page.wait_for_timeout(300) or route.continue_()
    ))
    hp.submit_spawn()

    # During the spawn, button is disabled with new label
    expect(hp.spawn_go).to_be_disabled(timeout=2000)
    expect(hp.spawn_go).to_have_text(re.compile(r"Starting"))

    # And eventually we enter chat
    expect(logged_in_page.locator("body")).to_have_class(
        re.compile(r"\bhas-session\b"), timeout=10000
    )
