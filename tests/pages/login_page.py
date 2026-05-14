"""Page object for the login view (#view-login)."""
from __future__ import annotations

from playwright.sync_api import Page, expect, Locator


class LoginPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.view: Locator = page.locator("#view-login")
        self.token_input: Locator = page.locator("#login-token")
        self.submit_btn: Locator = page.locator("#login-go")
        self.error: Locator = page.locator("#login-err")
        self.home_view: Locator = page.locator("#view-home")

    def expect_visible(self) -> None:
        expect(self.view).to_be_visible()

    def fill_token(self, token: str) -> "LoginPage":
        self.token_input.fill(token)
        return self

    def submit(self) -> "LoginPage":
        self.submit_btn.click()
        return self

    def press_enter(self) -> "LoginPage":
        self.token_input.press("Enter")
        return self

    def expect_error(self, contains: str | None = None) -> None:
        expect(self.error).to_be_visible()
        if contains is not None:
            expect(self.error).to_contain_text(contains)

    def expect_landed_on_home(self) -> None:
        expect(self.home_view).to_be_visible()
        expect(self.view).to_be_hidden()

    def stored_token(self) -> str | None:
        return self.page.evaluate('() => localStorage.getItem("ccr.token")')
