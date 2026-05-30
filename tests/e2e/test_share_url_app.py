"""App 端 share_url API — spec §17.

POST /api/share          owner 创建 share (path → id, url)
GET  /api/share          owner 列出自己 share
DELETE /api/share/<id>   owner 撤销
GET  /api/share/<id>     公开 (无 auth) 流式下载 — 这条仅被 hub forward 调用,
                         但 app 端直跑也应该 work, 因为它要 work 才有意义.
"""
from __future__ import annotations

import os
import tempfile
import time

import httpx


def _new_tmpfile(content: bytes = b"hello share\n", suffix: str = ".txt") -> str:
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix,
    ) as f:
        f.write(content)
        return f.name


def test_create_share_returns_id_and_url(base_url, test_token):
    p = _new_tmpfile()
    try:
        r = httpx.post(
            f"{base_url}/api/share",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"path": p},
            timeout=5,
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert "id" in j and len(j["id"]) >= 12, j
        # 默认永不过期
        assert j.get("expires_at") in (None, 0), j
        # url 可以含 short_host 或不含 (取决于 server 是否拿到 hub_origin)
        # 至少 id 在 URL 末段
        assert j["id"] in (j.get("url") or ""), j
    finally:
        os.unlink(p)


def test_create_share_requires_absolute_path(base_url, test_token):
    r = httpx.post(
        f"{base_url}/api/share",
        headers={"Authorization": f"Bearer {test_token}"},
        json={"path": "relative/foo.txt"},
        timeout=5,
    )
    assert r.status_code == 400, r.text


def test_create_share_404_for_missing(base_url, test_token):
    r = httpx.post(
        f"{base_url}/api/share",
        headers={"Authorization": f"Bearer {test_token}"},
        json={"path": "/tmp/__never_exists_xxx__.bin"},
        timeout=5,
    )
    assert r.status_code in (400, 404), r.text


def test_get_share_public_streams_file(base_url, test_token):
    """GET /api/share/<id> 公开 — 无 auth header. 流式回原文件内容 +
    Content-Disposition attachment + filename (basename only)."""
    content = b"file body for share\n" * 64
    p = _new_tmpfile(content=content)
    try:
        r = httpx.post(
            f"{base_url}/api/share",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"path": p, "note": "test"},
        )
        r.raise_for_status()
        sid = r.json()["id"]

        # 公开 GET, 不带 auth
        rr = httpx.get(f"{base_url}/api/share/{sid}", timeout=5)
        assert rr.status_code == 200, rr.text
        assert rr.content == content
        cd = rr.headers.get("content-disposition", "")
        assert "attachment" in cd
        # filename 只用 basename, 不暴露完整路径
        assert os.path.basename(p) in cd
        assert os.path.dirname(p) not in cd
    finally:
        os.unlink(p)


def test_list_shares_owner_only(base_url, test_token):
    p = _new_tmpfile()
    try:
        httpx.post(
            f"{base_url}/api/share",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"path": p, "note": "alpha"},
        ).raise_for_status()
        r = httpx.get(
            f"{base_url}/api/share",
            headers={"Authorization": f"Bearer {test_token}"},
            timeout=5,
        )
        assert r.status_code == 200, r.text
        shares = r.json()["shares"]
        assert any(s.get("note") == "alpha" for s in shares), shares
        # owner 查询应有 path 字段; 公开 GET 不应有
        assert any(s.get("path") for s in shares), "owner 列表应含 path"

        # 无 auth 不能列
        r2 = httpx.get(f"{base_url}/api/share", timeout=5)
        assert r2.status_code in (401, 403)
    finally:
        os.unlink(p)


def test_delete_share_revokes_url(base_url, test_token):
    p = _new_tmpfile(b"will be revoked\n")
    try:
        r = httpx.post(
            f"{base_url}/api/share",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"path": p},
        )
        sid = r.json()["id"]
        # 先确认能下
        rr = httpx.get(f"{base_url}/api/share/{sid}")
        assert rr.status_code == 200, rr.text

        # 撤销
        rd = httpx.delete(
            f"{base_url}/api/share/{sid}",
            headers={"Authorization": f"Bearer {test_token}"},
        )
        assert rd.status_code == 200, rd.text

        # 撤销后 404
        rr2 = httpx.get(f"{base_url}/api/share/{sid}")
        assert rr2.status_code == 404
    finally:
        os.unlink(p)


def test_get_share_404_when_file_deleted_after(base_url, test_token):
    p = _new_tmpfile(b"gone\n")
    r = httpx.post(
        f"{base_url}/api/share",
        headers={"Authorization": f"Bearer {test_token}"},
        json={"path": p},
    )
    sid = r.json()["id"]
    os.unlink(p)   # 文件没了
    rr = httpx.get(f"{base_url}/api/share/{sid}")
    assert rr.status_code == 404, rr.text


def test_get_share_404_when_expired(base_url, test_token):
    p = _new_tmpfile(b"about to expire\n")
    try:
        r = httpx.post(
            f"{base_url}/api/share",
            headers={"Authorization": f"Bearer {test_token}"},
            json={"path": p, "expires_in_sec": 1},
        )
        sid = r.json()["id"]
        time.sleep(1.5)
        rr = httpx.get(f"{base_url}/api/share/{sid}")
        assert rr.status_code == 404, rr.text
    finally:
        os.unlink(p)


def test_get_share_unknown_id_404(base_url):
    """无 auth, 不存在的 id → 404 (不暴露"曾经存在过")."""
    rr = httpx.get(f"{base_url}/api/share/000000000000000000000000")
    assert rr.status_code == 404


def test_create_share_requires_auth(base_url):
    r = httpx.post(
        f"{base_url}/api/share",
        json={"path": "/tmp/x"},
    )
    assert r.status_code in (401, 403)
