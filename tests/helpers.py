"""Small HTTP helpers — bypass the UI when seeding test state."""
from __future__ import annotations

import httpx


def api_spawn(base_url: str, token: str, cwd: str, name: str = "") -> str:
    """POST /api/spawn, return new session id."""
    r = httpx.post(
        f"{base_url}/api/spawn",
        headers={"Authorization": f"Bearer {token}"},
        json={"cwd": cwd, "name": name},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["id"]


def api_list_sessions(base_url: str, token: str) -> list[dict]:
    r = httpx.get(
        f"{base_url}/api/sessions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["sessions"]


def api_delete_session(base_url: str, token: str, sid: str) -> None:
    r = httpx.delete(
        f"{base_url}/api/sessions/{sid}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    r.raise_for_status()
