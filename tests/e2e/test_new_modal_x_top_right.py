"""§2.2: #modal-new-session 的 × 关闭按钮必须在 modal-head 的右上角,
不能挤在标题旁边."""
from __future__ import annotations

from playwright.sync_api import expect

from tests.pages.home_page import HomePage


def test_new_modal_close_x_is_top_right(logged_in_page):
    page = logged_in_page
    hp = HomePage(page)
    hp.expect_visible()
    hp.open_new_modal()
    modal = page.locator("#modal-new-session .modal")
    x = page.locator("#new-modal-close")
    title = page.locator("#modal-new-session .modal-title")
    m_box = modal.bounding_box()
    x_box = x.bounding_box()
    t_box = title.bounding_box()
    assert m_box and x_box and t_box
    # X right edge should be ≤ 20px from modal right edge (near corner)
    gap_right = (m_box["x"] + m_box["width"]) - (x_box["x"] + x_box["width"])
    assert 0 <= gap_right <= 20, (
        f"× should hug modal right edge, got gap={gap_right}"
    )
    # Title's text starts on the left edge of modal-head (cannot rely on
    # bbox since flex:1 makes its bbox span the full row). What matters
    # visually: X is on the right (already asserted above) and title is
    # not the right-aligned element. Sanity: X must be RIGHT of title's
    # bbox left edge.
    assert x_box["x"] > t_box["x"], "× must be to the right of the title"
