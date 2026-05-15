"""
Generates ~/SynologyDrive/Claude/ccr-corner-report/ — an HTML report
showing the SPEC design alongside the actual rendered output, with
pixel grids and per-pixel sampling.
"""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import expect

from tests.helpers import api_delete_session, api_spawn
from tests.pages.home_page import HomePage


REPORT_DIR = Path.home() / "SynologyDrive" / "Claude" / "ccr-corner-report"


def _hex_to_rgb(h):
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _resolve_colors(page):
    return page.evaluate(
        """() => {
            const cs = getComputedStyle(document.documentElement);
            return {
              bg_page: cs.getPropertyValue('--bg-page').trim(),
              bg_elev: cs.getPropertyValue('--bg-elev').trim(),
              border:  cs.getPropertyValue('--border').trim(),
              border_soft: cs.getPropertyValue('--border-soft').trim(),
            };
        }"""
    )


def _grid_html(img: Image.Image, label_pos=None):
    """Render the image as an HTML table of colored cells (one td per pixel)."""
    w, h = img.size
    rows = ['<table class="pixgrid">']
    for y in range(h):
        cells = ['<tr>']
        for x in range(w):
            r, g, b = img.getpixel((x, y))[:3]
            hexc = f"#{r:02x}{g:02x}{b:02x}"
            title = f"({x},{y}) rgb({r},{g},{b})"
            cells.append(f'<td style="background:{hexc}" title="{title}"></td>')
        cells.append("</tr>")
        rows.append("".join(cells))
    rows.append("</table>")
    return "\n".join(rows)


def _img_to_data_uri(img: Image.Image, scale=1):
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def selected_card(wide_page, base_url, test_token):
    sid = api_spawn(base_url, test_token, "/tmp", "report-card")
    hp = HomePage(wide_page)
    hp.expect_visible()
    card = hp.card_by_id(sid)
    expect(card).to_be_visible(timeout=5000)
    card.click()
    expect(card).to_have_class(re.compile(r"\bis-current\b"), timeout=5000)
    wide_page.wait_for_timeout(200)
    try:
        yield card, sid
    finally:
        api_delete_session(base_url, test_token, sid)


def test_generate_corner_report(wide_page, selected_card):
    """Generate the HTML report. Always passes; the artifact is the
    HTML at REPORT_DIR."""
    card, sid = selected_card
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    colors = _resolve_colors(wide_page)
    box = card.bounding_box()
    card_right = int(round(box["x"] + box["width"]))
    card_top = int(round(box["y"]))
    card_bottom = int(round(box["y"] + box["height"]))

    # Top-right corner: 40x40 sample (current AA-on state)
    tr_clip = {"x": card_right - 20, "y": card_top - 20, "width": 40, "height": 40}
    tr_png = wide_page.screenshot(clip=tr_clip)
    tr_img = Image.open(io.BytesIO(tr_png)).convert("RGB")

    # Bottom-right corner: 40x40 sample (current AA-on state)
    br_clip = {"x": card_right - 20, "y": card_bottom - 20, "width": 40, "height": 40}
    br_png = wide_page.screenshot(clip=br_clip)
    br_img = Image.open(io.BytesIO(br_png)).convert("RGB")

    # === AA-OFF variant: swap the inline SVG's paths to use
    # shape-rendering='crispEdges' and stroke aligned to integer pixels.
    # We do this by injecting a <style> that redefines --ccr-bulge-tr/br with
    # the AA-disabled SVG, then take another screenshot, then revert. ===
    aa_off_css = """
      :root {
        --ccr-bulge-tr: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 11' preserveAspectRatio='none' shape-rendering='crispEdges'><path d='M 0 0 L 10 0 A 10 10 0 0 1 0 10 Z' fill='%23f5f5f7' shape-rendering='crispEdges'/><path d='M 0 10 A 10 10 0 0 0 10 0' fill='none' stroke='%23d2d2d7' stroke-width='1' shape-rendering='crispEdges'/></svg>");
        --ccr-bulge-br: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 11' preserveAspectRatio='none' shape-rendering='crispEdges'><path d='M 0 11 L 10 11 A 10 10 0 0 0 0 1 Z' fill='%23f5f5f7' shape-rendering='crispEdges'/><path d='M 0 1 A 10 10 0 0 1 10 11' fill='none' stroke='%23d2d2d7' stroke-width='1' shape-rendering='crispEdges'/></svg>");
      }
    """
    style_id = "ccr-test-aaoff"
    wide_page.evaluate(
        """({css, id}) => {
            const s = document.createElement('style');
            s.id = id;
            s.textContent = css;
            document.head.appendChild(s);
        }""",
        {"css": aa_off_css, "id": style_id},
    )
    wide_page.wait_for_timeout(150)
    tr_img_off = Image.open(io.BytesIO(wide_page.screenshot(clip=tr_clip))).convert("RGB")
    br_img_off = Image.open(io.BytesIO(wide_page.screenshot(clip=br_clip))).convert("RGB")

    # Also try a CSS-level pixel-snapped alternative: a SOLID 1px ring
    # painted with conic-style hard-edge gradient. For comparison.
    wide_page.evaluate(
        "(id) => { const el = document.getElementById(id); if (el) el.remove(); }",
        style_id,
    )
    wide_page.wait_for_timeout(100)

    # Wider context: the full right edge of the card + 20px above/below + chat
    ctx_clip = {
        "x": max(0, card_right - 100),
        "y": max(0, card_top - 30),
        "width": 200,
        "height": (card_bottom - card_top) + 60,
    }
    ctx_png = wide_page.screenshot(clip=ctx_clip)
    ctx_img = Image.open(io.BytesIO(ctx_png)).convert("RGB")

    # Pseudo geometry for diagnostics
    geom = wide_page.evaluate(
        f"""() => {{
            const c = document.querySelector(`[data-id='{sid}']`);
            const cb = c.getBoundingClientRect();
            const bs = getComputedStyle(c, '::before');
            const as_ = getComputedStyle(c, '::after');
            return {{
              card: {{x: cb.x, y: cb.y, right: cb.right, bottom: cb.bottom,
                      w: cb.width, h: cb.height}},
              before: {{top: bs.top, right: bs.right, w: bs.width, h: bs.height,
                        bg_color: bs.backgroundColor,
                        bg_image: bs.backgroundImage}},
              after: {{bottom: as_.bottom, right: as_.right, w: as_.width, h: as_.height}},
            }};
        }}"""
    )

    # SPEC design — load 15.2 SVG by snipping from the SPEC file
    spec_path = Path.home() / "SynologyDrive" / "Claude" / "ccr-spec.html"
    spec_html_full = spec_path.read_text() if spec_path.exists() else ""

    # Find the 15.2 SVG block (the 660x660 one)
    spec_svg = ""
    m = re.search(
        r'<svg class="ui-svg" width="660" height="660".*?</svg>',
        spec_html_full,
        re.DOTALL,
    )
    if m:
        spec_svg = m.group(0)

    # Helper: dump per-row pixel grid as <pre>
    def _row_dump(img):
        w, h = img.size
        def code(rgb):
            r = rgb[0]
            if r >= 252: return "."
            if r >= 240: return "e"
            if r >= 225: return "g"
            if r >= 200: return "B"
            return "#"
        lines = []
        lines.append("    " + "".join(str(x % 10) for x in range(w)))
        for y in range(h):
            lines.append(
                f"{y:3d} " + "".join(code(img.getpixel((x, y))) for x in range(w))
            )
        return "\n".join(lines)

    # Sample table — annotated pixel checks
    def _sample(img, label, x, y, expect_hex):
        actual = img.getpixel((x, y))[:3]
        exp_rgb = _hex_to_rgb(expect_hex)
        diff = max(abs(a - b) for a, b in zip(actual, exp_rgb))
        ok = diff <= 12
        return {
            "label": label, "x": x, "y": y,
            "actual": actual, "expected": exp_rgb,
            "expected_hex": expect_hex, "diff": diff, "ok": ok,
        }

    border_hex = colors["border"]
    bg_elev_hex = colors["bg_elev"]
    bg_page_hex = colors["bg_page"]

    tr_samples = [
        _sample(tr_img, "Card body interior",     10, 25, bg_page_hex),
        _sample(tr_img, "Chat panel",             25, 15, bg_page_hex),
        _sample(tr_img, "Sidebar far above card", 5,  5,  bg_elev_hex),
        _sample(tr_img, "A — card top border (far left)", 6,  20, border_hex),
        _sample(tr_img, "F — divider (above bulge)",      19, 6,  border_hex),
        _sample(tr_img, "Inside bulge (upper-left)",      12, 12, bg_elev_hex),
        _sample(tr_img, "Outside bulge / chat",           19, 21, bg_page_hex),
    ]
    br_samples = [
        _sample(br_img, "Card body interior",      10, 10, bg_page_hex),
        _sample(br_img, "Chat panel",              25, 15, bg_page_hex),
        _sample(br_img, "Sidebar below bulge",     5,  35, bg_elev_hex),
        _sample(br_img, "A' — card bottom border", 6,  19, border_hex),
        _sample(br_img, "F' — divider (below bulge)", 19, 34, border_hex),
        _sample(br_img, "Inside bulge (lower-left)",  12, 28, bg_elev_hex),
    ]

    html = []
    html.append("""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>CCR §15.2 selected-card bulge — design vs actual</title>
<style>
body { font: 14px/1.5 -apple-system, "Helvetica Neue", system-ui, sans-serif; margin: 30px; color: #1f2328; background: #ffffff; max-width: 1400px; }
h1 { font-size: 22px; margin: 0 0 8px; }
h2 { font-size: 18px; margin: 32px 0 8px; padding-top: 12px; border-top: 1px solid #d2d2d7; }
h3 { font-size: 14px; margin: 14px 0 6px; color: #59636e; text-transform: uppercase; letter-spacing: .05em; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start; }
.box { border: 1px solid #d2d2d7; padding: 12px; border-radius: 8px; }
.box.bad { border-color: #cf222e; background: #fff5f5; }
img.snap { image-rendering: pixelated; image-rendering: crisp-edges; background: repeating-conic-gradient(#f5f5f5 0% 25%, transparent 0% 50%) 50% / 16px 16px; max-width: 100%; }
.pixgrid { border-collapse: collapse; image-rendering: pixelated; }
.pixgrid td { width: 12px; height: 12px; padding: 0; border: 1px solid rgba(0,0,0,0.05); }
pre.dump { font: 11px/1.2 ui-monospace, "SF Mono", Consolas, monospace; background: #f6f8fa; padding: 8px; border-radius: 6px; overflow-x: auto; }
table.samples { border-collapse: collapse; margin: 8px 0; width: 100%; }
table.samples th, table.samples td { border: 1px solid #d2d2d7; padding: 6px 10px; text-align: left; vertical-align: middle; }
table.samples th { background: #f6f8fa; font-weight: 600; font-size: 12px; }
.swatch { display: inline-block; width: 18px; height: 18px; border: 1px solid #d2d2d7; vertical-align: middle; margin-right: 6px; }
.ok { color: #1f883d; font-weight: 600; }
.bad { color: #cf222e; font-weight: 600; }
.meta { color: #59636e; font-size: 12px; }
.spec-frame { border: 1px solid #d2d2d7; padding: 8px; background: #ffffff; border-radius: 6px; }
.spec-frame svg { max-width: 100%; height: auto; display: block; }
pre.css { font: 11px/1.4 ui-monospace, "SF Mono", Consolas, monospace; background: #f6f8fa; padding: 10px; border-radius: 6px; overflow-x: auto; max-height: 280px; }
.legend { font-size: 12px; color: #59636e; margin: 6px 0 12px; }
</style></head><body>
""")
    html.append("<h1>§15.2 selected-card bulge — design vs actual</h1>")
    html.append('<p class="meta">Generated by tests/e2e/test_corner_report.py. ')
    html.append('Captured at <code>device_scale_factor=1</code> in chromium @ <code>1280×720</code>. ')
    html.append('All zoomed views use nearest-neighbor upscaling so individual CSS pixels remain visible.</p>')

    # Resolved CSS variables
    html.append('<h2>Resolved CSS variables</h2>')
    html.append('<table class="samples"><tr><th>variable</th><th>value</th><th>swatch</th></tr>')
    for k, v in colors.items():
        rgb = ", ".join(str(x) for x in _hex_to_rgb(v)) if v.startswith("#") else v
        html.append(f'<tr><td><code>--{k.replace("_","-")}</code></td><td><code>{v}</code> ≡ rgb({rgb})</td><td><span class="swatch" style="background:{v}"></span></td></tr>')
    html.append('</table>')

    # Card geometry diagnostics
    html.append('<h2>Card &amp; pseudo geometry</h2>')
    html.append('<pre class="dump">')
    import json
    html.append(json.dumps(geom, indent=2))
    html.append('</pre>')
    sub_pixel_h = (geom["card"]["h"] % 1) != 0
    sub_pixel_y = (geom["card"]["y"] % 1) != 0
    sub_pixel_b = (geom["card"]["bottom"] % 1) != 0
    if sub_pixel_h or sub_pixel_y or sub_pixel_b:
        html.append('<p class="bad">⚠ Sub-pixel card height detected (h={:.4f}px). '
                    'CSS rendering of the card border will be slightly anti-aliased '
                    'across two pixel rows; pixel-sample alignment is ±0.5px.</p>'
                    .format(geom["card"]["h"]))

    # Wider context shot
    html.append('<h2>Context (full card right edge + chat)</h2>')
    html.append(f'<img class="snap" src="{_img_to_data_uri(ctx_img, scale=2)}" alt="context">')

    # Top-right corner: SPEC + actual + AA-off comparison + grid + samples
    html.append('<h2>§15.2 Top-right corner</h2>')
    html.append('<div class="pair">')
    html.append('<div class="box"><h3>SPEC §15.2 (target)</h3>')
    html.append('<div class="spec-frame">')
    if spec_svg:
        html.append(spec_svg)
    else:
        html.append('<p class="bad">SPEC SVG not found.</p>')
    html.append('</div>')
    html.append('<p class="legend">Labels (A–F) match the §15.2 table contract: A/C/F visible 1px gray, B/D/E hidden.</p>')
    html.append('</div>')

    html.append('<div class="box"><h3>Actual rendered output (current — AA on)</h3>')
    html.append(f'<img class="snap" src="{_img_to_data_uri(tr_img, scale=20)}" alt="topright AA on">')
    html.append('<p class="meta">40×40 px → 20× nearest-neighbor. SVG paths use the default <code>shape-rendering: auto</code> so the browser anti-aliases.</p>')
    html.append('</div>')
    html.append('</div>')

    html.append('<div class="pair" style="margin-top: 12px">')
    html.append('<div class="box"><h3>AA OFF (shape-rendering: crispEdges)</h3>')
    html.append(f'<img class="snap" src="{_img_to_data_uri(tr_img_off, scale=20)}" alt="topright AA off">')
    html.append('<p class="meta">Same SVG but every <code>&lt;path&gt;</code> annotated <code>shape-rendering="crispEdges"</code>. Each pixel is either fully filled with the path color or not painted at all — no in-between blends.</p>')
    html.append('</div>')
    html.append('<div class="box"><h3>AA ON vs OFF — overlay diff</h3>')
    # diff image
    diff_img = Image.new("RGB", tr_img.size)
    for y in range(tr_img.height):
        for x in range(tr_img.width):
            a = tr_img.getpixel((x, y))
            b = tr_img_off.getpixel((x, y))
            d = max(abs(a[i] - b[i]) for i in range(3))
            if d == 0:
                diff_img.putpixel((x, y), (255, 255, 255))
            elif d < 20:
                diff_img.putpixel((x, y), (255, 230, 180))
            else:
                diff_img.putpixel((x, y), (220, 50, 50))
    html.append(f'<img class="snap" src="{_img_to_data_uri(diff_img, scale=20)}" alt="diff">')
    html.append('<p class="meta">White = same pixel. Orange = small difference (anti-alias noise). Red = ≥20 channel difference (where AA actually changed the pixel).</p>')
    html.append('</div>')
    html.append('</div>')

    html.append('<h3>Pixel grid — AA on (hover for rgb)</h3>')
    html.append(_grid_html(tr_img))

    html.append('<h3>Pixel grid — AA off</h3>')
    html.append(_grid_html(tr_img_off))

    html.append('<h3>ASCII row dump — AA on (. white / e bg-elev / g transition / B var(--border) / # dark)</h3>')
    html.append('<pre class="dump">' + _row_dump(tr_img) + '</pre>')

    html.append('<h3>ASCII row dump — AA off</h3>')
    html.append('<pre class="dump">' + _row_dump(tr_img_off) + '</pre>')

    html.append('<h3>Per-pixel checks</h3>')
    html.append('<table class="samples"><tr><th>label</th><th>(x, y)</th><th>expected</th><th>actual</th><th>diff</th><th>status</th></tr>')
    for s in tr_samples:
        actual_hex = "#{:02x}{:02x}{:02x}".format(*s["actual"])
        cls = "ok" if s["ok"] else "bad"
        sym = "✓" if s["ok"] else "✗"
        html.append(f'<tr><td>{s["label"]}</td><td>({s["x"]}, {s["y"]})</td>'
                    f'<td><span class="swatch" style="background:{s["expected_hex"]}"></span>'
                    f'{s["expected_hex"]} = rgb{tuple(s["expected"])}</td>'
                    f'<td><span class="swatch" style="background:{actual_hex}"></span>'
                    f'{actual_hex} = rgb{s["actual"]}</td>'
                    f'<td>{s["diff"]}</td><td class="{cls}">{sym}</td></tr>')
    html.append('</table>')

    # Bottom-right corner
    html.append('<h2>§15.2 Bottom-right corner</h2>')
    html.append('<div class="pair">')
    html.append('<div class="box"><h3>AA on (current)</h3>')
    html.append(f'<img class="snap" src="{_img_to_data_uri(br_img, scale=20)}" alt="bottomright AA on">')
    html.append('</div>')
    html.append('<div class="box"><h3>AA off (crispEdges)</h3>')
    html.append(f'<img class="snap" src="{_img_to_data_uri(br_img_off, scale=20)}" alt="bottomright AA off">')
    html.append('</div>')
    html.append('</div>')

    html.append('<h3>Pixel grid — AA on</h3>')
    html.append(_grid_html(br_img))

    html.append('<h3>Pixel grid — AA off</h3>')
    html.append(_grid_html(br_img_off))

    html.append('<h3>ASCII row dump — AA on</h3>')
    html.append('<pre class="dump">' + _row_dump(br_img) + '</pre>')

    html.append('<h3>ASCII row dump — AA off</h3>')
    html.append('<pre class="dump">' + _row_dump(br_img_off) + '</pre>')

    html.append('<h3>Per-pixel checks</h3>')
    html.append('<table class="samples"><tr><th>label</th><th>(x, y)</th><th>expected</th><th>actual</th><th>diff</th><th>status</th></tr>')
    for s in br_samples:
        actual_hex = "#{:02x}{:02x}{:02x}".format(*s["actual"])
        cls = "ok" if s["ok"] else "bad"
        sym = "✓" if s["ok"] else "✗"
        html.append(f'<tr><td>{s["label"]}</td><td>({s["x"]}, {s["y"]})</td>'
                    f'<td><span class="swatch" style="background:{s["expected_hex"]}"></span>'
                    f'{s["expected_hex"]} = rgb{tuple(s["expected"])}</td>'
                    f'<td><span class="swatch" style="background:{actual_hex}"></span>'
                    f'{actual_hex} = rgb{s["actual"]}</td>'
                    f'<td>{s["diff"]}</td><td class="{cls}">{sym}</td></tr>')
    html.append('</table>')

    # CSS dump
    html.append('<h2>Current CSS</h2>')
    css_path = Path.home() / "codes" / "ClaudeCodeRemoteAutoTest" / "ClaudeCodeRemote" / "claude_code_remote" / "server" / "static" / "style.css"
    css = css_path.read_text() if css_path.exists() else ""
    # Pull out the relevant blocks
    blocks = []
    for marker, name in [
        ("--ccr-bulge-tr:", "Light theme SVG variables"),
        (".session-card.is-current::before", ".is-current pseudo CSS"),
        ("body.stage-app #view-home::after", "Divider F"),
    ]:
        idx = css.find(marker)
        if idx >= 0:
            # find the surrounding block
            start = css.rfind("\n", 0, idx) + 1
            end = css.find("}", idx) + 1
            if "/* §15.2" in css[max(0, idx-200):idx]:
                start = css.rfind("/* §15.2", 0, idx)
            blocks.append((name, css[start:end+1]))
    for name, snippet in blocks[:3]:
        html.append(f'<h3>{name}</h3>')
        html.append(f'<pre class="css">{snippet}</pre>')

    html.append('<p class="meta">Test source: <code>tests/e2e/test_corner_pixels.py</code> + '
                '<code>tests/e2e/test_corner_report.py</code>. '
                'CSS source: <code>claude_code_remote/server/static/style.css</code>.</p>')
    html.append('</body></html>')

    report_path = REPORT_DIR / "index.html"
    report_path.write_text("\n".join(html))

    # Also save raw images
    tr_img.save(REPORT_DIR / "topright_raw.png")
    tr_img.resize((tr_img.width * 20, tr_img.height * 20), Image.NEAREST).save(REPORT_DIR / "topright_20x.png")
    br_img.save(REPORT_DIR / "bottomright_raw.png")
    br_img.resize((br_img.width * 20, br_img.height * 20), Image.NEAREST).save(REPORT_DIR / "bottomright_20x.png")
    ctx_img.save(REPORT_DIR / "context.png")

    print(f"\n=== Report written to {report_path} ===")
    print(f"   share via https://claude.hwaipy.cn/files/synology/Claude/ccr-corner-report/")
