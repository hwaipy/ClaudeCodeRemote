"""反向 tunnel 协议帧 (ccr-hub-spec.html §2).

App ↔ Hub 之间一条持久 WebSocket, 上面跑这些 JSON 帧. 每帧一个独立 JSON 对象,
type 字段做 discriminator.

双方都 import 同一份 schema, 保证编 / 解一致. 协议演进通过 TUNNEL_PROTO_VERSION
在握手 hello 帧带版本, 不兼容时直接 reject.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

TUNNEL_PROTO_VERSION = "0.1"


class _FrameBase(BaseModel):
    stream_id: str   # uuid / 递增 int / "*" (无具体流)


# ---------- HTTP ----------

class HttpReq(_FrameBase):
    type: Literal["http_req"] = "http_req"
    method: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    query: str = ""
    body_b64: str = ""
    user_id: str | None = None


class HttpRes(_FrameBase):
    type: Literal["http_res"] = "http_res"
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body_b64: str = ""


class HttpBody(_FrameBase):
    """大 body 分片 — body_b64 太大时 (>1MB) 用这个 stream 出来."""
    type: Literal["http_body"] = "http_body"
    body_b64: str
    final: bool = False


# ---------- WebSocket (forwarded user WS) ----------

class WsOpen(_FrameBase):
    type: Literal["ws_open"] = "ws_open"
    path: str
    user_id: str | None = None
    query: str = ""
    headers: dict[str, str] = Field(default_factory=dict)


class WsMsg(_FrameBase):
    type: Literal["ws_msg"] = "ws_msg"
    payload_b64: str
    is_binary: bool = False


class WsClose(_FrameBase):
    type: Literal["ws_close"] = "ws_close"
    code: int = 1000
    reason: str = ""


# ---------- Control / liveness ----------

class Ping(_FrameBase):
    type: Literal["ping"] = "ping"


class Pong(_FrameBase):
    type: Literal["pong"] = "pong"


class Control(_FrameBase):
    """握手 / metadata sync 等带外指令. op 字段决定子语义.

    定义的 op:
      - "hello"             app → hub, 携 app_name + version + capabilities
      - "ready"             hub → app, ack hello, 含 app_id + user_id
      - "sessions_snapshot" app → hub, 全量 sessions list
      - "session_added"     app → hub, 单 session 增量
      - "session_state"     app → hub, state 变更
      - "session_meta"      app → hub, name/cwd/model 等变更
      - "session_touch"     app → hub, last_active 推迟 (throttled)
      - "session_removed"   app → hub, 删除
      - "app_status"        hub → user (转推), app online/offline 状态
    """
    type: Literal["control"] = "control"
    op: str
    data: dict[str, Any] = Field(default_factory=dict)


AnyFrame = Annotated[
    Union[
        HttpReq, HttpRes, HttpBody,
        WsOpen, WsMsg, WsClose,
        Ping, Pong,
        Control,
    ],
    Field(discriminator="type"),
]

_adapter: TypeAdapter[AnyFrame] = TypeAdapter(AnyFrame)


def encode(frame: _FrameBase) -> str:
    """frame → JSON string."""
    return frame.model_dump_json()


def decode(raw: str | bytes) -> AnyFrame:
    """JSON string → frame instance. 抛 ValidationError 当 type 未知或缺字段."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    return _adapter.validate_json(raw)
