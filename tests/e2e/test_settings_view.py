"""Spec §2.3: ⚙ settings opens a slide-in settings view.

Contract:
- click #settings-btn → #view-settings gains .active
- back button or Esc removes .active
- default cwd / default permission read/write localStorage
- new-session modal pre-fills from those defaults
"""
from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.pages.home_page import HomePage

ACTIVE = re.compile(r"\bactive\b")


def test_settings_btn_opens_view(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator("#view-settings")).not_to_have_class(ACTIVE)
    logged_in_page.locator("#settings-btn").click()
    expect(logged_in_page.locator("#view-settings")).to_have_class(ACTIVE, timeout=2000)
    # Wait for the 420ms slide animation to settle
    logged_in_page.wait_for_timeout(500)
    box = logged_in_page.locator("#view-settings").bounding_box()
    assert box and box["x"] <= 5, f"view-settings should be on-screen: {box}"


def test_settings_head_matches_chat_style(logged_in_page):
    """Settings head has the same anatomy as chat head: back arrow + title."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    logged_in_page.locator("#settings-btn").click()
    back = logged_in_page.locator("#settings-back")
    expect(back).to_be_visible()
    expect(back).to_have_text("←")
    expect(logged_in_page.locator("#view-settings .settings-head .name"))\
        .to_have_text("Settings")


def test_settings_back_closes(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    logged_in_page.locator("#settings-btn").click()
    expect(logged_in_page.locator("#view-settings")).to_have_class(ACTIVE)
    logged_in_page.locator("#settings-back").click()
    expect(logged_in_page.locator("#view-settings")).not_to_have_class(ACTIVE)


def test_default_cwd_persists_to_localStorage(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    logged_in_page.locator("#settings-btn").click()
    logged_in_page.locator("#settings-default-cwd").fill("/tmp/mydefault")
    # Storage written on input
    val = logged_in_page.evaluate('() => localStorage.getItem("ccr.defaultCwd")')
    assert val == "/tmp/mydefault", f"expected stored, got {val!r}"


def test_default_perm_persists_and_marks_active(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    logged_in_page.locator("#settings-btn").click()
    # Default = manual
    expect(
        logged_in_page.locator("#settings-default-perm .spawn-perm-btn.active")
    ).to_have_attribute("data-mode", "manual")
    # Pick allow_all
    logged_in_page.locator(
        "#settings-default-perm .spawn-perm-btn[data-mode='allow_all']"
    ).click()
    expect(
        logged_in_page.locator("#settings-default-perm .spawn-perm-btn.active")
    ).to_have_attribute("data-mode", "allow_all")
    stored = logged_in_page.evaluate(
        '() => localStorage.getItem("ccr.defaultPermMode")'
    )
    assert stored == "allow_all", f"expected allow_all, got {stored!r}"


def test_new_session_modal_uses_settings_defaults(logged_in_page, tmp_path):
    """After setting defaults in settings view, opening the new-session
    modal pre-fills spawn-cwd + selects spawn-perm accordingly."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    # Configure defaults in settings
    logged_in_page.locator("#settings-btn").click()
    logged_in_page.locator("#settings-default-cwd").fill(str(tmp_path))
    logged_in_page.locator(
        "#settings-default-perm .spawn-perm-btn[data-mode='accept_edits']"
    ).click()
    logged_in_page.locator("#settings-back").click()
    expect(logged_in_page.locator("#view-settings")).not_to_have_class(ACTIVE)
    # Open new-session modal
    logged_in_page.locator("#new-btn").click()
    expect(logged_in_page.locator("#modal-new-session")).to_be_visible()
    expect(logged_in_page.locator("#spawn-cwd")).to_have_value(str(tmp_path))
    expect(
        logged_in_page.locator("#spawn-perm .spawn-perm-btn.active")
    ).to_have_attribute("data-mode", "accept_edits")
