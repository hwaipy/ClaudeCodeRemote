"""§3: empty directory shows centered 'empty' placeholder (not the old
"(no subdirectories)" text); §2.3: settings view supports left-edge
right-swipe to dismiss, identical to chat view."""
from __future__ import annotations

import os
import re

import pytest
from playwright.sync_api import expect

from tests.pages.home_page import HomePage

ACTIVE = re.compile(r"\bactive\b")


@pytest.fixture
def empty_dir(tmp_path):
    """A guaranteed-empty directory for the browse modal."""
    d = tmp_path / "absolutely-empty"
    d.mkdir()
    return d


def test_browse_empty_dir_says_empty_centered(logged_in_page, empty_dir):
    page = logged_in_page
    hp = HomePage(page)
    hp.expect_visible()
    hp.open_new_modal()
    # Aim browse at the known-empty dir
    page.locator("#spawn-cwd").fill(str(empty_dir))
    page.locator("#browse-btn").click()
    expect(page.locator("#modal-browse")).to_be_visible()
    empty = page.locator("#modal-browse .modal-empty")
    # Wait for the loading placeholder to be replaced by the empty placeholder
    expect(empty).to_have_text(re.compile(r"^\s*empty\s*$", re.I), timeout=3000)
    # Center horizontally + fill remaining vertical space (so the text
    # sits in the middle of the empty area, not at the top).
    list_box = page.locator("#modal-browse #modal-list").bounding_box()
    empty_box = empty.bounding_box()
    assert list_box and empty_box
    list_cx = list_box["x"] + list_box["width"] / 2
    empty_cx = empty_box["x"] + empty_box["width"] / 2
    assert abs(list_cx - empty_cx) <= 20, (
        f"empty placeholder not horizontally centered: list_cx={list_cx}, "
        f"empty_cx={empty_cx}"
    )
    # Empty placeholder should take MOST of the list area (after any
    # parent ".." row). Proves flex:1 + min-height:100% applied so the
    # text sits inside a tall centered region, not collapsed to its
    # text height (~20px).
    list_h = list_box["height"]
    assert empty_box["height"] >= list_h * 0.6, (
        f"empty placeholder should fill the empty space: empty_h="
        f"{empty_box['height']}, list_h={list_h}"
    )


def _swipe(page, view_sel, start_x=10, end_x=900, y=400, steps=10):
    """Dispatch synthetic touchstart / touchmove / touchend on view_sel,
    simulating a left-edge right-swipe."""
    page.evaluate(
        """([sel, sx, ex, yc, n]) => {
          const view = document.querySelector(sel);
          if (!view) throw new Error("no view " + sel);
          function fire(type, x, y) {
            const t = new Touch({
              identifier: 1, target: view, clientX: x, clientY: y,
              pageX: x, pageY: y,
            });
            const ev = new TouchEvent(type, {
              bubbles: true, cancelable: true,
              touches:        type === "touchend" ? [] : [t],
              targetTouches:  type === "touchend" ? [] : [t],
              changedTouches: [t],
            });
            view.dispatchEvent(ev);
          }
          fire("touchstart", sx, yc);
          for (let i = 1; i <= n; i++) {
            const x = sx + (ex - sx) * i / n;
            fire("touchmove", x, yc);
          }
          fire("touchend", ex, yc);
        }""",
        [view_sel, start_x, end_x, y, steps],
    )


def test_settings_view_dismisses_on_right_swipe(logged_in_page):
    page = logged_in_page
    hp = HomePage(page)
    hp.expect_visible()
    page.locator("#settings-btn").click()
    expect(page.locator("#view-settings")).to_have_class(ACTIVE, timeout=2000)
    page.wait_for_timeout(500)   # let slide-in settle

    # Swipe right from left edge → view-settings should lose .active
    vw = page.viewport_size["width"]
    _swipe(page, "#view-settings", start_x=10, end_x=vw - 50, y=400, steps=10)
    # Allow the 260ms commit animation to finish
    page.wait_for_timeout(400)
    expect(page.locator("#view-settings")).not_to_have_class(ACTIVE)


def test_settings_view_stays_when_swipe_too_short(logged_in_page):
    """Tiny swipe (under 35% threshold) should bounce back, not dismiss."""
    page = logged_in_page
    hp = HomePage(page)
    hp.expect_visible()
    page.locator("#settings-btn").click()
    expect(page.locator("#view-settings")).to_have_class(ACTIVE, timeout=2000)
    page.wait_for_timeout(500)

    vw = page.viewport_size["width"]
    # Only swipe 5% of the width — way below 35% threshold
    _swipe(page, "#view-settings", start_x=10, end_x=int(vw * 0.05) + 10,
           y=400, steps=5)
    page.wait_for_timeout(400)
    expect(page.locator("#view-settings")).to_have_class(ACTIVE)
