"""§2 Stash 区: 三档 Active / Stash / Inactive 的完整行为契约."""
from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


def _stash(base_url, token, sid):
    r = httpx.post(
        f"{base_url}/api/sessions/{sid}/stash",
        headers={"Authorization": f"Bearer {token}"}, timeout=5,
    )
    r.raise_for_status()


def _activate(base_url, token, sid):
    r = httpx.post(
        f"{base_url}/api/sessions/{sid}/activate",
        headers={"Authorization": f"Bearer {token}"}, timeout=5,
    )
    r.raise_for_status()


def _deactivate(base_url, token, sid):
    r = httpx.post(
        f"{base_url}/api/sessions/{sid}/deactivate",
        headers={"Authorization": f"Bearer {token}"}, timeout=5,
    )
    r.raise_for_status()


def _session_record(base_url, token, sid):
    r = httpx.get(
        f"{base_url}/api/sessions",
        headers={"Authorization": f"Bearer {token}"}, timeout=5,
    )
    r.raise_for_status()
    for s in r.json()["sessions"]:
        if s["id"] == sid:
            return s
    return None


def test_stash_section_exists_and_default_expanded(logged_in_page):
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    box = logged_in_page.locator("#sessions-stash")
    expect(box).to_be_visible()
    cls = box.get_attribute("class") or ""
    assert "expanded" in cls, f"Stash section must default to expanded: {cls!r}"
    # Order: Active above Stash above Inactive
    a = logged_in_page.locator("#sessions-active").bounding_box()
    s = logged_in_page.locator("#sessions-stash").bounding_box()
    i = logged_in_page.locator("#sessions-inactive").bounding_box()
    assert a and s and i
    assert a["y"] < s["y"] < i["y"], (
        f"order must be Active < Stash < Inactive: "
        f"a.y={a['y']}, s.y={s['y']}, i.y={i['y']}"
    )


def test_stash_action_moves_card_to_stash(
    logged_in_page, base_url, test_token
):
    sid = api_spawn(base_url, test_token, "/tmp", "stash-me")
    try:
        hp = HomePage(logged_in_page)
        hp.expect_visible()
        card = logged_in_page.locator(f"[data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        # Click kebab → Stash
        card.locator(".card-menu-btn").click()
        card.locator(".card-menu-item[data-action='stash']").click()
        # Wait for ws push to move it; check the card now lives in #session-list-stash
        expect(
            logged_in_page.locator(f"#session-list-stash [data-id='{sid}']")
        ).to_be_visible(timeout=3000)
        # And not in active list anymore
        expect(
            logged_in_page.locator(f"#session-list-active [data-id='{sid}']")
        ).to_have_count(0)
        # Server confirms is_stash
        rec = _session_record(base_url, test_token, sid)
        assert rec and rec.get("is_stash") and not rec.get("is_inactive")
    finally:
        api_delete_session(base_url, test_token, sid)


def test_activate_from_stash_moves_back_to_active(
    logged_in_page, base_url, test_token
):
    sid = api_spawn(base_url, test_token, "/tmp", "stash-back")
    try:
        _stash(base_url, test_token, sid)
        hp = HomePage(logged_in_page)
        hp.expect_visible()
        # Card should land in stash list
        expect(
            logged_in_page.locator(f"#session-list-stash [data-id='{sid}']")
        ).to_be_visible(timeout=5000)
        # Click kebab → Activate (now visible in stash section)
        card = logged_in_page.locator(f"#session-list-stash [data-id='{sid}']")
        card.locator(".card-menu-btn").click()
        card.locator(".card-menu-item[data-action='activate']").click()
        # Card should move back to active list
        expect(
            logged_in_page.locator(f"#session-list-active [data-id='{sid}']")
        ).to_be_visible(timeout=3000)
        rec = _session_record(base_url, test_token, sid)
        assert rec and not rec.get("is_stash") and not rec.get("is_inactive")
    finally:
        api_delete_session(base_url, test_token, sid)


def test_stash_and_inactive_are_mutually_exclusive(base_url, test_token):
    """Setting stash on an inactive session should clear inactive flag,
    and vice versa. Pure server-side contract."""
    sid = api_spawn(base_url, test_token, "/tmp", "exclusive")
    try:
        _deactivate(base_url, test_token, sid)
        rec = _session_record(base_url, test_token, sid)
        assert rec["is_inactive"] and not rec["is_stash"]
        # Now stash → must clear inactive
        _stash(base_url, test_token, sid)
        rec = _session_record(base_url, test_token, sid)
        assert rec["is_stash"] and not rec["is_inactive"], (
            f"stash should have cleared inactive: {rec}"
        )
        # Deactivate again → must clear stash
        _deactivate(base_url, test_token, sid)
        rec = _session_record(base_url, test_token, sid)
        assert rec["is_inactive"] and not rec["is_stash"], (
            f"deactivate should have cleared stash: {rec}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_deactivate_from_stash_moves_to_inactive(
    logged_in_page, base_url, test_token
):
    """Spec §2 Stash 区: stash 卡 kebab 有 Deactivate, 点击 → 卡移到
    Inactive 区, server-side 同时清 stashed_at (互斥)."""
    sid = api_spawn(base_url, test_token, "/tmp", "stash-deactivate")
    try:
        _stash(base_url, test_token, sid)
        hp = HomePage(logged_in_page)
        hp.expect_visible()
        # Wait for the stash card to render
        card = logged_in_page.locator(f"#session-list-stash [data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        card.locator(".card-menu-btn").click()
        # Deactivate item exists in stash kebab now
        expect(
            card.locator(".card-menu-item[data-action='deactivate']")
        ).to_be_visible()
        card.locator(".card-menu-item[data-action='deactivate']").click()
        # Card now lives in Inactive list (which may be collapsed in DOM —
        # query directly, no visibility expectation)
        expect(
            logged_in_page.locator(f"#session-list-inactive [data-id='{sid}']")
        ).to_have_count(1, timeout=3000)
        expect(
            logged_in_page.locator(f"#session-list-stash [data-id='{sid}']")
        ).to_have_count(0)
        # Server: is_inactive=true AND is_stash=false
        rec = _session_record(base_url, test_token, sid)
        assert rec and rec["is_inactive"] and not rec["is_stash"], (
            f"after deactivate from stash: {rec}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_stash_from_inactive_moves_to_stash(
    logged_in_page, base_url, test_token
):
    """Inactive 卡 kebab 有 Stash, 点击 → 卡移到 Stash 区, server 同时
    清 deactivated_at (互斥). 对称于 stash → deactivate 路径."""
    sid = api_spawn(base_url, test_token, "/tmp", "inactive-to-stash")
    try:
        _deactivate(base_url, test_token, sid)
        hp = HomePage(logged_in_page)
        hp.expect_visible()
        # Expand inactive so we can see/click the card's kebab
        logged_in_page.evaluate(
            "() => document.getElementById('sessions-inactive').classList.add('expanded')"
        )
        card = logged_in_page.locator(f"#session-list-inactive [data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        card.locator(".card-menu-btn").click()
        # Stash item exists on inactive kebab
        expect(
            card.locator(".card-menu-item[data-action='stash']")
        ).to_be_visible()
        card.locator(".card-menu-item[data-action='stash']").click()
        # Card moves to Stash list
        expect(
            logged_in_page.locator(f"#session-list-stash [data-id='{sid}']")
        ).to_be_visible(timeout=3000)
        expect(
            logged_in_page.locator(f"#session-list-inactive [data-id='{sid}']")
        ).to_have_count(0)
        # Server: is_stash=true AND is_inactive=false
        rec = _session_record(base_url, test_token, sid)
        assert rec and rec["is_stash"] and not rec["is_inactive"], (
            f"after stash from inactive: {rec}"
        )
    finally:
        api_delete_session(base_url, test_token, sid)


def test_stash_first_load_default_expanded(logged_in_page):
    """设备首次加载 (localStorage 没有 ccr.stashOpen) → Stash 展开."""
    # Ensure clean slate
    logged_in_page.evaluate('() => localStorage.removeItem("ccr.stashOpen")')
    logged_in_page.reload()
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    expect(logged_in_page.locator("#sessions-stash"))\
        .to_have_class(re.compile(r"\bexpanded\b"))


def test_stash_persists_collapse_then_expand(logged_in_page):
    """点 toggle → 收起 → 持久化 "0" → reload 仍收起.
    再 toggle → 展开 → 持久化 "1" → reload 仍展开."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    box = logged_in_page.locator("#sessions-stash")
    # Start from clean expanded state
    logged_in_page.evaluate('() => localStorage.removeItem("ccr.stashOpen")')
    logged_in_page.reload()
    hp.expect_visible()
    expect(box).to_have_class(re.compile(r"\bexpanded\b"))
    # Toggle to collapse
    box.locator(".stash-toggle").click()
    expect(box).not_to_have_class(re.compile(r"\bexpanded\b"))
    stored = logged_in_page.evaluate(
        '() => localStorage.getItem("ccr.stashOpen")'
    )
    assert stored == "0", f"expected '0' after collapse, got {stored!r}"
    # Reload: still collapsed
    logged_in_page.reload()
    hp.expect_visible()
    expect(logged_in_page.locator("#sessions-stash"))\
        .not_to_have_class(re.compile(r"\bexpanded\b"))
    # Toggle back to expand
    logged_in_page.locator("#sessions-stash .stash-toggle").click()
    stored = logged_in_page.evaluate(
        '() => localStorage.getItem("ccr.stashOpen")'
    )
    assert stored == "1", f"expected '1' after expand, got {stored!r}"


def test_inactive_never_persists_and_starts_collapsed(logged_in_page):
    """Inactive 区: 每次加载都是收起. 即使上轮用户展开过, reload
    后还是收起 — 完全不写 localStorage."""
    hp = HomePage(logged_in_page)
    hp.expect_visible()
    box = logged_in_page.locator("#sessions-inactive")
    # Initial state: collapsed
    expect(box).not_to_have_class(re.compile(r"\bexpanded\b"))
    # User expands it
    box.locator(".inactive-toggle").click()
    expect(box).to_have_class(re.compile(r"\bexpanded\b"))
    # No localStorage write
    stored = logged_in_page.evaluate(
        '() => localStorage.getItem("ccr.inactiveOpen")'
    )
    assert stored is None, (
        f"inactive must NOT persist; got localStorage.ccr.inactiveOpen={stored!r}"
    )
    # Reload: back to collapsed
    logged_in_page.reload()
    hp.expect_visible()
    expect(logged_in_page.locator("#sessions-inactive"))\
        .not_to_have_class(re.compile(r"\bexpanded\b"))


def test_inactive_card_kebab_has_rename(
    logged_in_page, base_url, test_token
):
    """新增: Inactive 卡的菜单也得有 Rename."""
    sid = api_spawn(base_url, test_token, "/tmp", "inactive-rename")
    try:
        _deactivate(base_url, test_token, sid)
        hp = HomePage(logged_in_page)
        hp.expect_visible()
        # Expand inactive section so the card is visible
        logged_in_page.evaluate(
            "() => document.getElementById('sessions-inactive').classList.add('expanded')"
        )
        card = logged_in_page.locator(f"#session-list-inactive [data-id='{sid}']")
        expect(card).to_be_visible(timeout=5000)
        card.locator(".card-menu-btn").click()
        expect(
            card.locator(".card-menu-item[data-action='rename']")
        ).to_be_visible()
    finally:
        api_delete_session(base_url, test_token, sid)
