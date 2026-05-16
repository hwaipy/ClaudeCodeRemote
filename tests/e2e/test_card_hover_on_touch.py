"""§2: .session-card hover effect must NOT stick on touch devices.

Mobile browsers leave `:hover` applied to the last-tapped element after
touchend; the only reliable fix is gating hover rules with the
`@media (hover: hover)` media query. This module verifies BOTH the CSS
structure (white-box) and the runtime behavior under a touch-enabled
browser context (black-box)."""
from __future__ import annotations

import re
import pathlib

import pytest


def test_card_hover_rule_is_gated_by_media_hover_hover():
    """White-box: locate the `.session-card:hover` rule in style.css and
    confirm it lives INSIDE a `@media (hover: hover)` block."""
    css = pathlib.Path(
        "claude_code_remote/server/static/style.css"
    ).read_text()
    # Find every `@media (hover: hover) { ... }` block (non-greedy)
    # and check the base .session-card:hover rule appears inside at least
    # one of them.
    blocks = re.findall(r"@media\s*\(\s*hover:\s*hover\s*\)\s*\{(.*?)\}\s*",
                        css, flags=re.S)
    inside = any(
        re.search(r"\.session-card:hover\s*\{", b) for b in blocks
    )
    assert inside, (
        ".session-card:hover must live inside @media (hover: hover) — "
        "otherwise mobile browsers leave the hover state stuck on the "
        "last-tapped card."
    )
    # Also: the bare `.session-card:hover` rule (NOT under hover-only)
    # must not be present.
    stripped = re.sub(
        r"@media\s*\(\s*hover:\s*hover\s*\)\s*\{.*?\n\}\s*", "",
        css, flags=re.S,
    )
    bare = re.search(r"^\s*\.session-card:hover\s*\{", stripped, re.M)
    assert not bare, (
        "Found a `.session-card:hover` rule OUTSIDE the (hover: hover) "
        "guard — it will stick on touch devices."
    )


@pytest.fixture
def touch_page(playwright, base_url, test_token):
    """A page in a context that has hasTouch=true and isMobile=true —
    so the `(hover: none)` media query matches and `(hover: hover)`
    doesn't."""
    browser = playwright.chromium.launch()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
    )
    page = ctx.new_page()
    page.goto(base_url)
    page.fill("#login-token", test_token)
    page.click("#login-go")
    page.wait_for_selector("#view-home.active", timeout=5000)
    yield page
    ctx.close()
    browser.close()


def test_hover_rule_does_not_match_on_touch_device(touch_page, base_url, test_token):
    """In a hasTouch/isMobile context, the @media (hover: hover) block
    must NOT match, so .session-card:hover never triggers."""
    page = touch_page
    # Verify the media query truly evaluates to false in this context
    matches_hover = page.evaluate(
        "() => window.matchMedia('(hover: hover)').matches"
    )
    matches_none = page.evaluate(
        "() => window.matchMedia('(hover: none)').matches"
    )
    assert matches_hover is False, (
        f"in touch context, (hover: hover) must NOT match, got {matches_hover}"
    )
    assert matches_none is True, (
        f"in touch context, (hover: none) must match, got {matches_none}"
    )
