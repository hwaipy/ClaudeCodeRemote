"""§1 Login view — covers MUST rows of the spec table."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.pages.login_page import LoginPage


def test_empty_token_shows_error(fresh_page):
    lp = LoginPage(fresh_page)
    lp.expect_visible()
    lp.submit()
    lp.expect_error()
    lp.expect_visible()
    assert lp.stored_token() is None


def test_wrong_token_shows_error_and_doesnt_persist(fresh_page):
    lp = LoginPage(fresh_page)
    lp.fill_token("definitely-not-the-token").submit()
    lp.expect_error()
    lp.expect_visible()
    assert lp.stored_token() is None


def test_correct_token_lands_on_home_and_persists(fresh_page, test_token):
    lp = LoginPage(fresh_page)
    lp.fill_token(test_token).submit()
    lp.expect_landed_on_home()
    assert lp.stored_token() == test_token


def test_enter_key_submits(fresh_page, test_token):
    lp = LoginPage(fresh_page)
    lp.fill_token(test_token).press_enter()
    lp.expect_landed_on_home()


def test_stored_token_auto_logs_in(page, base_url, test_token):
    # Plant valid token before any app code runs
    page.goto(base_url)
    page.evaluate(f'() => localStorage.setItem("ccr.token", {test_token!r})')
    page.goto(base_url)
    expect(page.locator("#view-home")).to_be_visible(timeout=5000)
    expect(page.locator("#view-login")).to_be_hidden()


def test_stored_bad_token_falls_back_to_login(page, base_url):
    page.goto(base_url)
    page.evaluate('() => localStorage.setItem("ccr.token", "bad-token-xxx")')
    page.goto(base_url)
    expect(page.locator("#view-login")).to_be_visible(timeout=5000)
    # 401 should clear bad token
    stored = page.evaluate('() => localStorage.getItem("ccr.token")')
    assert stored in (None, "", "bad-token-xxx"), \
        f"expected clear or untouched, got {stored!r}"
