"""Spec §2: .home-top must be position: sticky — when the session list
scrolls, the three icons (settings / new / search) stay pinned at the
top of the home view."""
from __future__ import annotations

from playwright.sync_api import expect

from tests.helpers import api_spawn, api_delete_session
from tests.pages.home_page import HomePage


def test_home_top_uses_position_sticky(logged_in_page):
    """Computed style check: position must be 'sticky'."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    pos = logged_in_page.evaluate(
        '() => getComputedStyle(document.querySelector(".home-top")).position'
    )
    assert pos == "sticky", f"home-top must be sticky, got {pos!r}"


def test_home_top_stays_at_top_when_scrolled(
    logged_in_page, base_url, test_token
):
    """Create many sessions, scroll the home view, then check that the
    home-top's top edge is still at (or very near) the top of its scroll
    container — proving it stuck instead of scrolling away."""
    page = logged_in_page
    # Spawn enough sessions to make the list scrollable
    sids = []
    try:
        for i in range(15):
            sids.append(api_spawn(base_url, test_token, "/tmp", f"sticky-{i}"))
        hp = HomePage(page)
        hp.expect_visible()
        # Wait for cards to render
        page.wait_for_function(
            "() => document.querySelectorAll('.session-card').length >= 5",
            timeout=4000,
        )

        scroller_sel = "#view-home > .center-wrap"
        # Scroll the home scroll container down a fair amount
        page.evaluate(
            f"() => document.querySelector('{scroller_sel}').scrollTop = 400"
        )
        page.wait_for_timeout(200)

        # The home-top's bounding box top should be at (or very near) the
        # scroller's top.  Anything > 50px below the scroller top means it
        # scrolled away with content — i.e. sticky is broken.
        sc_top = page.evaluate(
            f"() => document.querySelector('{scroller_sel}')"
            ".getBoundingClientRect().top"
        )
        ht_top = page.evaluate(
            '() => document.querySelector(".home-top").getBoundingClientRect().top'
        )
        delta = ht_top - sc_top
        # Tight tolerance: home-top's top edge MUST be flush with the
        # scroller top edge — no transparent gap above where cards could
        # leak through.  Previously .center-wrap had a 24-80px padding-top
        # that left a transparent region above home-top; this assertion
        # guards against that gap coming back.
        assert 0 <= delta <= 1, (
            f"home-top must sit flush with scroller top (no gap above): "
            f"sc_top={sc_top}, ht_top={ht_top}, delta={delta}"
        )
    finally:
        for sid in sids:
            try:
                api_delete_session(base_url, test_token, sid)
            except Exception:
                pass
