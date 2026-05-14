"""Page object for the home view (#view-home)."""
from __future__ import annotations

from playwright.sync_api import Page, expect, Locator


class HomePage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.view: Locator = page.locator("#view-home")
        self.list: Locator = page.locator("#session-list")
        self.cards: Locator = page.locator("#session-list .session-card")
        self.spawn_name: Locator = page.locator("#spawn-name")
        self.spawn_cwd: Locator = page.locator("#spawn-cwd")
        self.spawn_go: Locator = page.locator("#spawn-go")
        self.spawn_err: Locator = page.locator("#spawn-err")
        self.browse_btn: Locator = page.locator("#browse-btn")
        self.logout: Locator = page.locator("#logout")
        self.hard_reload: Locator = page.locator("#hard-reload")

    def expect_visible(self) -> None:
        expect(self.view).to_be_visible()
        # wait until WS-driven snapshot replaced the "Loading…" placeholder
        expect(self.list).not_to_contain_text("Loading…", timeout=5000)

    def open_new_modal(self) -> "HomePage":
        """Click ＋ New session to reveal the form (it's hidden by default)."""
        self.page.locator("#new-btn").click()
        self.page.locator("#modal-new-session").wait_for(state="visible")
        return self

    def card_by_id(self, session_id: str) -> Locator:
        return self.page.locator(f"#session-list .session-card[data-id='{session_id}']")

    def card_by_name(self, name: str) -> Locator:
        return self.page.locator(
            f"#session-list .session-card:has(.name:text-is('{name}'))"
        )

    def fill_spawn_form(self, cwd: str, name: str = "") -> "HomePage":
        """Opens the new-session modal (if not open) and fills the form."""
        if not self.page.locator("#modal-new-session:not([hidden])").count():
            self.open_new_modal()
        if name:
            self.spawn_name.fill(name)
        self.spawn_cwd.fill(cwd)
        return self

    def submit_spawn(self) -> "HomePage":
        self.spawn_go.click()
        return self

    def stored_token(self) -> str | None:
        return self.page.evaluate('() => localStorage.getItem("ccr.token")')
