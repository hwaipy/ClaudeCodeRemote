"""协议帧 schema 测试 (ccr-hub-spec.html §2).

反向 tunnel 双方按这个 schema 编 / 解 JSON 帧. 必须无歧义 + 向前兼容.
"""
from __future__ import annotations

import base64
import json

import pytest

from claude_code_remote.shared import tunnel_proto as tp


def _b64(s: bytes | str) -> str:
    if isinstance(s, str):
        s = s.encode()
    return base64.b64encode(s).decode("ascii")


# ---------- HttpReq ----------

def test_http_req_roundtrip():
    f = tp.HttpReq(
        stream_id="s1",
        method="GET",
        path="/api/sessions",
        headers={"accept": "application/json"},
        user_id="u1",
    )
    raw = tp.encode(f)
    back = tp.decode(raw)
    assert isinstance(back, tp.HttpReq)
    assert back.method == "GET"
    assert back.path == "/api/sessions"
    assert back.user_id == "u1"
    assert back.body_b64 == ""


def test_http_req_with_body():
    payload = b'{"hello":"world"}'
    f = tp.HttpReq(
        stream_id="s2",
        method="POST",
        path="/api/spawn",
        headers={"content-type": "application/json"},
        body_b64=_b64(payload),
    )
    back = tp.decode(tp.encode(f))
    assert isinstance(back, tp.HttpReq)
    assert base64.b64decode(back.body_b64) == payload


# ---------- HttpRes ----------

def test_http_res_roundtrip():
    f = tp.HttpRes(
        stream_id="s1",
        status=200,
        headers={"content-type": "application/json"},
        body_b64=_b64(b'[]'),
    )
    back = tp.decode(tp.encode(f))
    assert isinstance(back, tp.HttpRes)
    assert back.status == 200
    assert back.stream_id == "s1"


# ---------- WS frames ----------

def test_ws_open_roundtrip():
    f = tp.WsOpen(
        stream_id="w1",
        path="/ws/sid-123",
        user_id="u1",
    )
    back = tp.decode(tp.encode(f))
    assert isinstance(back, tp.WsOpen)
    assert back.path == "/ws/sid-123"


def test_ws_msg_text_payload():
    f = tp.WsMsg(
        stream_id="w1",
        payload_b64=_b64('{"type":"hello"}'),
        is_binary=False,
    )
    back = tp.decode(tp.encode(f))
    assert isinstance(back, tp.WsMsg)
    assert back.is_binary is False
    assert base64.b64decode(back.payload_b64) == b'{"type":"hello"}'


def test_ws_close_defaults():
    f = tp.WsClose(stream_id="w1")
    back = tp.decode(tp.encode(f))
    assert isinstance(back, tp.WsClose)
    assert back.code == 1000
    assert back.reason == ""


# ---------- Control / ping-pong ----------

def test_hello_control_frame():
    f = tp.Control(
        stream_id="*",
        op="hello",
        data={"app_name": "macmini-home", "version": "1", "capabilities": []},
    )
    back = tp.decode(tp.encode(f))
    assert isinstance(back, tp.Control)
    assert back.op == "hello"
    assert back.data["app_name"] == "macmini-home"


def test_ping_pong():
    p = tp.Ping(stream_id="*")
    raw = tp.encode(p)
    back = tp.decode(raw)
    assert isinstance(back, tp.Ping)
    q = tp.Pong(stream_id="*")
    assert isinstance(tp.decode(tp.encode(q)), tp.Pong)


# ---------- Discriminator ----------

def test_decode_unknown_type_raises():
    raw = json.dumps({"stream_id": "x", "type": "garbage", "extra": 1})
    with pytest.raises(Exception):
        tp.decode(raw)


def test_decode_missing_type_raises():
    raw = json.dumps({"stream_id": "x"})
    with pytest.raises(Exception):
        tp.decode(raw)


def test_decode_discriminator_routes_correctly():
    """各 type 字段必须不重复, decode 出来类型一致."""
    cases = [
        (tp.HttpReq(stream_id="s", method="GET", path="/x", headers={}), tp.HttpReq),
        (tp.HttpRes(stream_id="s", status=200, headers={}), tp.HttpRes),
        (tp.WsOpen(stream_id="s", path="/ws"), tp.WsOpen),
        (tp.WsMsg(stream_id="s", payload_b64=""), tp.WsMsg),
        (tp.WsClose(stream_id="s"), tp.WsClose),
        (tp.Ping(stream_id="s"), tp.Ping),
        (tp.Pong(stream_id="s"), tp.Pong),
        (tp.Control(stream_id="s", op="x"), tp.Control),
    ]
    for f, cls in cases:
        back = tp.decode(tp.encode(f))
        assert type(back) is cls, f"{type(back).__name__} != {cls.__name__}"


# ---------- Protocol version ----------

def test_protocol_version_exposed():
    """shared/tunnel_proto.py 必须暴露 TUNNEL_PROTO_VERSION 常量."""
    assert isinstance(tp.TUNNEL_PROTO_VERSION, str)
    assert tp.TUNNEL_PROTO_VERSION
