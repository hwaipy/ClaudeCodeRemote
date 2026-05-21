"""§16 PWA — manifest contents, icons reachable, service worker registers."""
from __future__ import annotations

import httpx


def test_manifest_content_type(base_url):
    r = httpx.get(f"{base_url}/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers.get("content-type", "")


def test_manifest_required_fields(base_url):
    r = httpx.get(f"{base_url}/manifest.webmanifest")
    data = r.json()
    assert data["display"] == "standalone"
    assert data["start_url"] == "./"
    assert data["scope"] == "./"
    assert data["name"]
    assert data["short_name"]
    assert data["theme_color"] == data["background_color"]
    sizes = {i["sizes"] for i in data["icons"]}
    assert "192x192" in sizes
    assert "512x512" in sizes


def test_icons_reachable(base_url):
    for path in ("static/icon-192.png", "static/icon-512.png", "icon.svg"):
        r = httpx.get(f"{base_url}/{path}")
        assert r.status_code == 200, f"{path}: {r.status_code}"


def test_sw_js_served(base_url):
    r = httpx.get(f"{base_url}/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "")
    # Header lets the SW control the whole scope
    assert "Service-Worker-Allowed" in r.headers


def test_sw_registers_on_window_load(logged_in_page):
    """Wait until navigator.serviceWorker has a registration on /."""
    logged_in_page.wait_for_function(
        """async () => {
            const r = await navigator.serviceWorker.getRegistration();
            return !!r;
        }""",
        timeout=10000,
    )


def test_static_assets_carry_build_id_query(base_url):
    """Index references static assets with ?v=<BUILD_ID> for cache busting."""
    r = httpx.get(base_url + "/")
    assert "?v=" in r.text, "expected cache-busting ?v= on static URLs"


def test_sw_uses_cache_first_strategy_with_manual_refresh_escape(base_url):
    """§16 SW 策略契约: 默认 cache-first (含 navigate / HTML), 仅在
    req.cache === 'reload' / 'no-cache' (用户手动刷新) 时走网络. 这样
    所有资源都从 cache 命中, 直到用户主动 refresh."""
    r = httpx.get(f"{base_url}/sw.js")
    src = r.text
    # 必须检测 req.cache 判断是否手动刷新
    assert 'req.cache === "reload"' in src or "req.cache === 'reload'" in src, (
        "SW must branch on req.cache to detect manual refresh; "
        "expected `req.cache === 'reload'` or `'no-cache'`"
    )
    assert '"no-cache"' in src or "'no-cache'" in src, (
        "SW must also treat req.cache === 'no-cache' as manual refresh"
    )
    # 必须保留 cache-first 兜底分支 (caches.match → cached || fetch)
    assert "caches.match" in src
    # /api/ 和 ws 必须跳过 SW
    assert "/api/" in src
    assert "ws-global" in src
