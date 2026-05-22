"""Hub passkey (FIDO2 / WebAuthn) 登录 + 注册.

API:
  POST /api/hub/passkey/register/start    (登录中) → 返 CreationOptions
  POST /api/hub/passkey/register/finish   (登录中) → 验证 attestation + 落 db
  POST /api/hub/passkey/login/start                → 返 RequestOptions
  POST /api/hub/passkey/login/finish               → 验证 assertion + 设 cookie
  GET    /api/hub/passkeys                (登录中) → list user 的 passkey
  DELETE /api/hub/passkeys/<id>           (登录中) → revoke 一个 passkey

Challenge: server-generated random bytes, 缓存在内存 dict (单进程 OK), 5 min TTL.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel
from webauthn import (
    generate_authentication_options, generate_registration_options,
    options_to_json, verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

from . import auth, db as hub_db

log = logging.getLogger("ccr.hub.passkey")

router = APIRouter(prefix="/api/hub")

# RP (Relying Party): WebAuthn 强校验 origin/host.
# CCR_HUB_RP_ID = hub.qpqi.group; CCR_HUB_ORIGIN = https://hub.qpqi.group
# 测试 / 本地用 localhost 默认.
RP_ID = os.environ.get("CCR_HUB_RP_ID", "localhost")
RP_NAME = os.environ.get("CCR_HUB_RP_NAME", "ClaudeCodeRemote Hub")
ORIGIN = os.environ.get("CCR_HUB_ORIGIN", "http://localhost")

# 内存 challenge cache (单进程 OK; 多进程换 redis)
# key = challenge (bytes b64url), value = {kind, user_id?, exp}
_CHALLENGES: dict[str, dict[str, Any]] = {}
_CHALLENGE_TTL_S = 300


def _purge_challenges() -> None:
    now = time.time()
    for k in list(_CHALLENGES.keys()):
        if _CHALLENGES[k]["exp"] < now:
            del _CHALLENGES[k]


def _save_challenge(challenge: bytes, kind: str,
                    user_id: str | None = None) -> None:
    _purge_challenges()
    key = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
    _CHALLENGES[key] = {
        "kind": kind, "user_id": user_id,
        "exp": time.time() + _CHALLENGE_TTL_S,
    }


def _consume_challenge(challenge_bytes: bytes,
                       kind: str) -> dict[str, Any] | None:
    """One-shot: pop + verify kind matches."""
    key = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode()
    entry = _CHALLENGES.pop(key, None)
    if entry is None or entry["exp"] < time.time():
        return None
    if entry["kind"] != kind:
        return None
    return entry


async def _require_user(session_id: str | None) -> str:
    user_id = await auth.get_user_id(session_id)
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user_id


# ---------- Registration ----------

class RegisterStartReq(BaseModel):
    nickname: str | None = None


@router.post("/passkey/register/start")
async def register_start(body: RegisterStartReq,
                         ccr_sess: str | None = Cookie(None)):
    user_id = await _require_user(ccr_sess)
    user = await hub_db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user_not_found")

    # 已有 passkey 全部 excludeCredentials, 防止重复绑同设备
    existing = await hub_db.list_passkeys_for_user(user_id)
    exclude = [
        PublicKeyCredentialDescriptor(
            id=base64.urlsafe_b64decode(_pad_b64(e["id"])),
        )
        for e in existing
    ]

    opts = generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=user_id.encode("utf-8"),
        user_name=user.get("email", user_id),
        user_display_name=user.get("email", user_id),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
        timeout=60_000,
    )
    _save_challenge(opts.challenge, kind="register", user_id=user_id)
    # 暂存 nickname 跟 challenge 一起 (finish 时取出)
    key = base64.urlsafe_b64encode(opts.challenge).rstrip(b"=").decode()
    _CHALLENGES[key]["nickname"] = (body.nickname or "").strip() or None
    # py-webauthn 直接给可序列化 dict
    return _json_response(opts)


class RegisterFinishReq(BaseModel):
    credential: dict[str, Any]


@router.post("/passkey/register/finish")
async def register_finish(body: RegisterFinishReq,
                          ccr_sess: str | None = Cookie(None)):
    user_id = await _require_user(ccr_sess)
    # 取 client_data 里的 challenge 反向 lookup
    try:
        client_data_json = base64.urlsafe_b64decode(
            _pad_b64(body.credential["response"]["clientDataJSON"]),
        )
        import json as _json
        client_data = _json.loads(client_data_json)
        challenge_b64 = client_data["challenge"]
        challenge_bytes = base64.urlsafe_b64decode(_pad_b64(challenge_b64))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"bad_credential: {e}")

    entry = _consume_challenge(challenge_bytes, kind="register")
    if not entry or entry.get("user_id") != user_id:
        raise HTTPException(status_code=400, detail="challenge_invalid")

    try:
        verification = verify_registration_response(
            credential=body.credential,
            expected_challenge=challenge_bytes,
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("register verify failed: %s", e)
        raise HTTPException(status_code=400, detail=f"verify_failed: {e}")

    credential_id_b64 = base64.urlsafe_b64encode(
        verification.credential_id,
    ).rstrip(b"=").decode()
    await hub_db.add_passkey(
        credential_id=credential_id_b64,
        user_id=user_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        transports=None,
        nickname=entry.get("nickname"),
    )
    return {"ok": True, "credential_id": credential_id_b64}


# ---------- Login ----------

@router.post("/passkey/login/start")
async def login_start():
    """Public. 我们用 discoverable credential (resident key), 所以不传具体
    allowCredentials — authenticator 列已注册的 passkey 给用户选."""
    opts = generate_authentication_options(
        rp_id=RP_ID,
        user_verification=UserVerificationRequirement.PREFERRED,
        timeout=60_000,
    )
    _save_challenge(opts.challenge, kind="login")
    return _json_response(opts)


class LoginFinishReq(BaseModel):
    credential: dict[str, Any]


@router.post("/passkey/login/finish")
async def login_finish(body: LoginFinishReq, response: Response):
    try:
        client_data_json = base64.urlsafe_b64decode(
            _pad_b64(body.credential["response"]["clientDataJSON"]),
        )
        import json as _json
        client_data = _json.loads(client_data_json)
        challenge_bytes = base64.urlsafe_b64decode(
            _pad_b64(client_data["challenge"]),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"bad_credential: {e}")

    entry = _consume_challenge(challenge_bytes, kind="login")
    if not entry:
        raise HTTPException(status_code=400, detail="challenge_invalid")

    credential_id_b64 = body.credential.get("id")
    if not credential_id_b64:
        raise HTTPException(status_code=400, detail="missing_credential_id")
    row = await hub_db.get_passkey(credential_id_b64)
    if not row:
        raise HTTPException(status_code=403, detail="unknown_credential")

    try:
        verification = verify_authentication_response(
            credential=body.credential,
            expected_challenge=challenge_bytes,
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("login verify failed: %s", e)
        raise HTTPException(status_code=403, detail=f"verify_failed: {e}")

    await hub_db.bump_passkey_sign_count(
        credential_id_b64, verification.new_sign_count,
    )
    sid = await auth.create_session(row["user_id"])
    response.set_cookie(
        "ccr_sess", sid,
        httponly=True, samesite="lax", path="/",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return {"ok": True, "user_id": row["user_id"]}


# ---------- Management ----------

@router.get("/passkeys")
async def list_passkeys(ccr_sess: str | None = Cookie(None)):
    user_id = await _require_user(ccr_sess)
    return await hub_db.list_passkeys_for_user(user_id)


@router.delete("/passkeys/{credential_id}")
async def delete_passkey(credential_id: str,
                         ccr_sess: str | None = Cookie(None)):
    user_id = await _require_user(ccr_sess)
    ok = await hub_db.delete_passkey(credential_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="passkey_not_found")
    return {"ok": True}


# ---------- helpers ----------

def _pad_b64(s: str) -> str:
    """base64url 不带 padding → 补 = 让 b64decode 接受."""
    return s + "=" * (-len(s) % 4)


def _json_response(opts):
    """py-webauthn options_to_json → 返 JSONResponse, 客户端可直接吃."""
    from fastapi.responses import JSONResponse
    import json as _json
    return JSONResponse(_json.loads(options_to_json(opts)))
