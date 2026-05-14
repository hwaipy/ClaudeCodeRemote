"""Page object for the directory browser modal (#modal-browse)."""
from __future__ import annotations

from playwright.sync_api import Page, expect, Locator


class DirectoryModal:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.modal: Locator = page.locator("#modal-browse")
        self.crumb: Locator = page.locator("#modal-crumb")
        self.list: Locator = page.locator("#modal-list")
        self.close_x: Locator = page.locator("#modal-close")
        self.cancel: Locator = page.locator("#modal-cancel")
        self.confirm: Locator = page.locator("#modal-confirm")
        self.newdir_btn: Locator = page.locator("#modal-newdir")

    def expect_open(self) -> None:
        expect(self.modal).to_be_visible()

    def expect_closed(self) -> None:
        expect(self.modal).to_be_hidden()

    def expect_crumb(self, path: str) -> None:
        expect(self.crumb).to_have_text(path)

    def rows(self) -> Locator:
        return self.modal.locator(".modal-row")

    def parent_row(self) -> Locator:
        return self.modal.locator(".modal-row.parent")

    def row_by_name(self, name: str) -> Locator:
        return self.modal.locator(f".modal-row:has(.name:text-is('{name}'))")
