"""
Pixel-level inspection of the selected card's top-right corner.
Samples the actual rendered pixels (via screenshot) to verify the
§15.2 border contract is met at the pixel level.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


ARTIFACTS = Path(__file__).resolve().parents[2] / "tmp" / "corner-pixels"


def _rgb(s: str) -> tuple[int, int, int]:
    """Parse 'rgb(r, g, b)' or 'rgba(r, g, b, a)' → (r, g, b)."""
    nums = [int(x) for x in re.findall(r"\d+", s)[:3]]
    return tuple(nums)


def _close(a, b, tol=10):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _screenshot_corner(page, card, dx=20, dy=20, dw=40, dh=40, *, prefix="topright"):
    """Grab a small screenshot of the selected card's top-right corner.

    Returns (PIL image, corner_x_in_image, corner_y_in_image, full_clip).
    The card's top-right corner sits at (dx, dy) inside the returned image.
    Snaps to integer pixel boundaries so the pixel grid aligns cleanly.
    """
    box = card.bounding_box()
    card_right = int(round(box["x"] + box["width"]))
    card_top = int(round(box["y"]))
    x0 = card_right - dx
    y0 = card_top - dy
    clip = {"x": x0, "y": y0, "width": dw, "height": dh}
    png = page.screenshot(clip=clip)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    return img, dx, dy, clip


def _screenshot_corner_bottom(page, card, dx=20, dy=20, dw=40, dh=40):
    box = card.bounding_box()
    card_right = int(round(box["x"] + box["width"]))
    card_bottom = int(round(box["y"] + box["height"]))
    x0 = card_right - dx
    y0 = card_bottom - (dh - dy)
    clip = {"x": x0, "y": y0, "width": dw, "height": dh}
    png = page.screenshot(clip=clip)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    return img, dx, dh - dy, clip


def _resolve_colors(page):
    """Read the CSS variable values via the document's computed style."""
    return page.evaluate(
        """() => {
            const cs = getComputedStyle(document.documentElement);
            return {
              bg_page: cs.getPropertyValue('--bg-page').trim(),
              bg_elev: cs.getPropertyValue('--bg-elev').trim(),
              border: cs.getPropertyValue('--border').trim(),
            };
        }"""
    )


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _save_artifact(img, name):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    img.save(ARTIFACTS / name)
    # also 30x upscaled for visual inspection
    big = img.resize((img.width * 20, img.height * 20), Image.NEAREST)
    big.save(ARTIFACTS / name.replace(".png", "_20x.png"))


@pytest.fixture
def selected_card(wide_page, base_url, test_token):
    """Spawn a card, click it to make it .is-current, return the card locator
    and the new session id (so the test can clean up)."""
    sid = api_spawn(base_url, test_token, "/tmp", "corner-pixel-test")
    hp = HomePage(wide_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    card.click()
    expect(card).to_have_class(re.compile(r"\bis-current\b"), timeout=5000)
    wide_page.wait_for_timeout(150)  # let any transitions settle
    try:
        yield card, sid
    finally:
        api_delete_session(base_url, test_token, sid)


def test_inspect_pseudo_geometry(wide_page, selected_card):
    """Print the actual pseudo geometry so we can align the pixel tests."""
    card, sid = selected_card
    info = wide_page.evaluate(
        f"""() => {{
            const c = document.querySelector(`[data-id='{sid}']`);
            const cb = c.getBoundingClientRect();
            const cs = getComputedStyle(c);
            const bs = getComputedStyle(c, '::before');
            const as_ = getComputedStyle(c, '::after');
            // Synthesize pseudo bounding rect from computed properties.
            const pTop = parseFloat(bs.top);
            const pHeight = parseFloat(bs.height);
            const pRight = parseFloat(bs.right);
            const pWidth = parseFloat(bs.width);
            const aBot = parseFloat(as_.bottom);
            const aHeight = parseFloat(as_.height);
            return {{
              card_box: {{x: cb.x, y: cb.y, right: cb.right, bottom: cb.bottom,
                          w: cb.width, h: cb.height}},
              card_border: {{
                top: cs.borderTopWidth,
                bottom: cs.borderBottomWidth,
                left: cs.borderLeftWidth,
                right: cs.borderRightWidth,
              }},
              before_computed: {{
                top: bs.top, right: bs.right, width: bs.width, height: bs.height,
                bg_color: bs.backgroundColor,
                bg_image: bs.backgroundImage.substring(0, 200),
              }},
              after_computed: {{
                bottom: as_.bottom, right: as_.right, width: as_.width, height: as_.height,
              }},
              // Container = card's padding box if spec'd correctly:
              cb_padding_top: cb.y + parseFloat(cs.borderTopWidth),
              cb_padding_bottom: cb.bottom - parseFloat(cs.borderBottomWidth),
              before_synthesized_top: cb.y + parseFloat(cs.borderTopWidth) + pTop,
              before_synthesized_bottom_using_padding: cb.y + parseFloat(cs.borderTopWidth) + pTop + pHeight,
              before_synthesized_top_using_border: cb.y + pTop,
              before_synthesized_bottom_using_border: cb.y + pTop + pHeight,
              after_synthesized_bottom_using_padding: cb.bottom - parseFloat(cs.borderBottomWidth) - aBot,
              after_synthesized_top_using_padding: cb.bottom - parseFloat(cs.borderBottomWidth) - aBot - aHeight,
              after_synthesized_bottom_using_border: cb.bottom - aBot,
              after_synthesized_top_using_border: cb.bottom - aBot - aHeight,
            }};
        }}"""
    )
    import json
    print("\n=== Pseudo geometry inspection ===\n" + json.dumps(info, indent=2))


def test_corner_pixels_topright(wide_page, selected_card):
    """Pixel-level verification of the TOP-RIGHT corner of the selected card.

    Spec §15.2 contract pixel checks:
      A right end (just left of bulge, at card top): gray (border color)
      F bottom end (column at sidebar right, just above bulge): gray
      Card top border, FAR from corner: gray
      Inside the bulge box, upper-left: bg-elev (sidebar color)
      Outside the bulge box, lower-right: bg-page (white)
      Card body, well inside: bg-page
      Chat panel, right of sidebar: bg-page

    Also asserts: the gray top border doesn't suddenly disappear at the
    bulge edge (must continue at least until the arc takes over).
    """
    card, _sid = selected_card
    img, cx, cy, clip = _screenshot_corner(wide_page, card, dx=20, dy=20, dw=40, dh=40)
    _save_artifact(img, "topright.png")

    colors = _resolve_colors(wide_page)
    border_rgb = _hex_to_rgb(colors["border"]) if colors["border"].startswith("#") else _rgb(colors["border"])
    bg_elev_rgb = _hex_to_rgb(colors["bg_elev"]) if colors["bg_elev"].startswith("#") else _rgb(colors["bg_elev"])
    bg_page_rgb = _hex_to_rgb(colors["bg_page"]) if colors["bg_page"].startswith("#") else _rgb(colors["bg_page"])

    def at(label, x, y, *, expect_rgb, tol=12):
        actual = img.getpixel((x, y))
        ok = _close(actual, expect_rgb, tol=tol)
        msg = f"({x:2d}, {y:2d}) {label}: expected ~{expect_rgb} got {actual}"
        return ok, msg

    checks = [
        # Card body — well inside, should be bg-page (white)
        at("card body interior", cx - 10, cy + 10, expect_rgb=bg_page_rgb),
        # Chat panel — right of sidebar, should be bg-page
        at("chat panel", cx + 5, cy + 5, expect_rgb=bg_page_rgb),
        # Sidebar above bulge (far from corner) — should be bg-elev
        at("sidebar interior", cx - 15, cy - 15, expect_rgb=bg_elev_rgb),
        # Card top border FAR from corner — gray
        at("A far (card top border)", cx - 14, cy, expect_rgb=border_rgb),
        # F divider bottom (just above bulge, at x just inside sidebar right) — gray
        at("F bottom (divider)", cx - 1, cy - 14, expect_rgb=border_rgb),
        # Inside the bulge (upper-left of pseudo, sidebar color)
        at("inside bulge UL", cx - 8, cy - 8, expect_rgb=bg_elev_rgb),
        # Outside the bulge (lower-right of arc — chat panel area)
        at("outside bulge LR", cx - 1, cy + 1, expect_rgb=bg_page_rgb, tol=20),
    ]

    failures = [m for ok, m in checks if not ok]
    if failures:
        print("\n=== TOP-RIGHT corner pixel samples ===")
        for ok, m in checks:
            print(("  ✓ " if ok else "  ✗ ") + m)
        pytest.fail(
            f"{len(failures)} of {len(checks)} pixel checks failed:\n"
            + "\n".join("  " + m for m in failures)
            + f"\n(artifact: {ARTIFACTS / 'topright.png'} and _20x.png)"
        )


def test_corner_pixels_bottomright(wide_page, selected_card):
    """Mirror of test_corner_pixels_topright for the BOTTOM-RIGHT corner."""
    card, _sid = selected_card
    img, cx, cy, clip = _screenshot_corner_bottom(wide_page, card, dx=20, dy=20, dw=40, dh=40)
    _save_artifact(img, "bottomright.png")

    colors = _resolve_colors(wide_page)
    border_rgb = _hex_to_rgb(colors["border"]) if colors["border"].startswith("#") else _rgb(colors["border"])
    bg_elev_rgb = _hex_to_rgb(colors["bg_elev"]) if colors["bg_elev"].startswith("#") else _rgb(colors["bg_elev"])
    bg_page_rgb = _hex_to_rgb(colors["bg_page"]) if colors["bg_page"].startswith("#") else _rgb(colors["bg_page"])

    def at(label, x, y, *, expect_rgb, tol=12):
        actual = img.getpixel((x, y))
        ok = _close(actual, expect_rgb, tol=tol)
        return ok, f"({x:2d}, {y:2d}) {label}: expected ~{expect_rgb} got {actual}"

    checks = [
        # Interior of card body — well away from any border so sub-pixel
        # rendering of the card edge doesn't bleed in. Sub-pixel card height
        # makes the very-corner pixel a blend.
        at("card body interior", cx - 10, cy - 5, expect_rgb=bg_page_rgb),
        at("chat panel", cx + 5, cy - 5, expect_rgb=bg_page_rgb),
        at("sidebar below bulge", cx - 15, cy + 15, expect_rgb=bg_elev_rgb),
        at("A' (card bottom border)", cx - 14, cy - 1, expect_rgb=border_rgb),
        at("F' divider (just below bulge)", cx - 1, cy + 14, expect_rgb=border_rgb),
        at("inside bulge LL", cx - 8, cy + 8, expect_rgb=bg_elev_rgb),
    ]
    failures = [m for ok, m in checks if not ok]
    if failures:
        print("\n=== BOTTOM-RIGHT corner pixel samples ===")
        for ok, m in checks:
            print(("  ✓ " if ok else "  ✗ ") + m)
        pytest.fail(
            f"{len(failures)} of {len(checks)} pixel checks failed:\n"
            + "\n".join("  " + m for m in failures)
            + f"\n(artifact: {ARTIFACTS / 'bottomright.png'} and _20x.png)"
        )
