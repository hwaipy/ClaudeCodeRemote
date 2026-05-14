"""L0 smoke tests — server boots, static assets reachable."""
from __future__ import annotations

import httpx
from playwright.sync_api import expect


def test_healthz(base_url):
    r = httpx.get(f"{base_url}/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_html(base_url):
    r = httpx.get(base_url + "/")
    assert r.status_code == 200
    assert "<title>ClaudeCodeRemote</title>" in r.text


def test_manifest(base_url):
    r = httpx.get(f"{base_url}/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers.get("content-type", "")


def test_sw_js(base_url):
    r = httpx.get(f"{base_url}/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "")


def test_sessions_requires_auth(base_url):
    r = httpx.get(f"{base_url}/api/sessions")
    assert r.status_code == 401


def test_sessions_with_token(base_url, test_token):
    r = httpx.get(f"{base_url}/api/sessions",
                  headers={"Authorization": f"Bearer {test_token}"})
    assert r.status_code == 200
    assert "sessions" in r.json()


def test_page_loads_login_view(fresh_page, base_url):
    expect(fresh_page.locator("#view-login")).to_be_visible()
    expect(fresh_page.locator("#view-home")).to_be_hidden()
    expect(fresh_page.locator("#view-chat")).to_be_hidden()
