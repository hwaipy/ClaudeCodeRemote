"""目录浏览 modal 必须固定大小: 切目录不改盒子尺寸. 旧行为是 max-height
随内容生长, 用户切目录时整个 modal 跳一下, 体验生硬."""
from __future__ import annotations

from playwright.sync_api import expect

from tests.pages.home_page import HomePage


def _open_browse_via_spawn(page) -> None:
    hp = HomePage(page)
    hp.expect_visible()
    hp.open_new_modal()
    page.locator("#browse-btn").click()
    expect(page.locator("#modal-browse")).to_be_visible()
    # Wait for first listing to render
    expect(page.locator("#modal-list .modal-row").first).to_be_visible(
        timeout=3000
    )


def test_browse_modal_size_stable_across_navigation(logged_in_page):
    """点目录切换前后, .modal 的高度必须完全一致 (±1px 容差)."""
    page = logged_in_page
    _open_browse_via_spawn(page)
    modal = page.locator("#modal-browse .modal")
    h_before = modal.bounding_box()["height"]
    w_before = modal.bounding_box()["width"]
    # Pick the first child directory (skip parent ".." row if present)
    rows = page.locator("#modal-list .modal-row").all()
    target = None
    for r in rows:
        cls = (r.get_attribute("class") or "")
        if "parent" not in cls:
            target = r
            break
    if not target:
        # No children to navigate into — skip the dimensional check
        return
    target.click()
    page.wait_for_timeout(150)
    h_after = modal.bounding_box()["height"]
    w_after = modal.bounding_box()["width"]
    assert abs(h_after - h_before) <= 1, (
        f"browse modal height must NOT change on navigation: "
        f"{h_before} → {h_after}"
    )
    assert abs(w_after - w_before) <= 1, (
        f"browse modal width must NOT change: {w_before} → {w_after}"
    )


def test_browse_modal_height_is_large(logged_in_page):
    """固定大小应该是 '比较大' — 至少 400px (避免回归到原来的 content-fit)."""
    page = logged_in_page
    _open_browse_via_spawn(page)
    h = page.locator("#modal-browse .modal").bounding_box()["height"]
    assert h >= 400, f"browse modal too small: {h}px (want ≥ 400)"
