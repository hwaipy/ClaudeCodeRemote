"""Hub /files/<short_host>/<fid> 端到端 — spec §17.

约束:
- short_host 在 app register 时由 hub 生成 + 落 apps.short_host (6 base62 唯一)
- GET /files/<short_host>/<fid>: 公开 (无 auth), 走 hub → tunnel → app /api/share/<fid>
- short_host 不存在 / app 不 online → 404 (不 503, 不暴露 app 存在性)
"""
from __future__ import annotations

import os
import re
import tempfile

import httpx


def _login(c, email, pw):
    c.post("/api/hub/login", json={"email": email, "password": pw}).raise_for_status()


def _get_app_meta(c) -> dict:
    """admin login 后, 拿当前 online app 信息 (含 short_host)."""
    apps = c.get("/api/hub/apps").json()
    online = [a for a in apps if a["online"]]
    assert online, f"no online app: {apps}"
    return online[0]


def _create_share_via_app(app_url: str, app_token: str, path: str) -> dict:
    """直接调 app 的 /api/share 拿 id (用 app 自己的 CCR_TOKEN)."""
    r = httpx.post(
        f"{app_url}/api/share",
        headers={"Authorization": f"Bearer {app_token}"},
        json={"path": path},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


def test_hub_assigns_short_host_to_app(hub_and_app):
    """app register 时 hub 应给它分配一个 short_host (6 base62)."""
    with httpx.Client(base_url=hub_and_app["hub_url"], timeout=5) as c:
        _login(c, hub_and_app["admin_email"], hub_and_app["admin_pw"])
        meta = _get_app_meta(c)
        sh = meta.get("short_host") or ""
        assert re.fullmatch(r"[A-Za-z0-9]{6}", sh), (
            f"short_host 应为 6 base62, 实际 {sh!r}"
        )


def test_files_route_forwards_to_app_share(hub_and_app):
    """GET /files/<short_host>/<id> 公开匿名, hub forward 到 app /api/share/<id>."""
    body = b"shared via hub\n" * 32
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(body)
        p = f.name
    try:
        share = _create_share_via_app(
            hub_and_app["app_url"], hub_and_app["app_token"], p,
        )
        fid = share["id"]

        with httpx.Client(base_url=hub_and_app["hub_url"], timeout=10) as c:
            _login(c, hub_and_app["admin_email"], hub_and_app["admin_pw"])
            meta = _get_app_meta(c)
            sh = meta["short_host"]

        # 公开访问 — 无 auth header, 无 cookie
        # (用一个全新 client 避免带上 login cookie)
        with httpx.Client(timeout=10) as anon:
            r = anon.get(f"{hub_and_app['hub_url']}/files/{sh}/{fid}")
            assert r.status_code == 200, r.text
            assert r.content == body
            cd = r.headers.get("content-disposition", "")
            assert "attachment" in cd
    finally:
        os.unlink(p)


def test_files_unknown_short_host_404(hub_and_app):
    with httpx.Client(timeout=5) as anon:
        r = anon.get(f"{hub_and_app['hub_url']}/files/Zzzz99/anything-id")
        assert r.status_code == 404


def test_files_unknown_fid_404(hub_and_app):
    """short_host 对, fid 没注册 → app 返 404, hub 透回."""
    with httpx.Client(base_url=hub_and_app["hub_url"], timeout=5) as c:
        _login(c, hub_and_app["admin_email"], hub_and_app["admin_pw"])
        meta = _get_app_meta(c)
        sh = meta["short_host"]
    with httpx.Client(timeout=5) as anon:
        r = anon.get(f"{hub_and_app['hub_url']}/files/{sh}/0000000000000000")
        assert r.status_code == 404


def test_files_route_does_not_require_auth(hub_and_app):
    """/files/* 完全公开 — 无 hub cookie, 无 Bearer 都能下."""
    body = b"public bytes"
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(body); p = f.name
    try:
        share = _create_share_via_app(
            hub_and_app["app_url"], hub_and_app["app_token"], p,
        )
        with httpx.Client(base_url=hub_and_app["hub_url"], timeout=5) as c:
            _login(c, hub_and_app["admin_email"], hub_and_app["admin_pw"])
            meta = _get_app_meta(c)
            sh = meta["short_host"]
        # 没任何 header/cookie
        r = httpx.get(f"{hub_and_app['hub_url']}/files/{sh}/{share['id']}",
                      timeout=5)
        assert r.status_code == 200
        assert r.content == body
    finally:
        os.unlink(p)
