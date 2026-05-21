"""反向 tunnel server — App ↔ Hub 长连 WS (ccr-hub-spec.html §2).

App 端用 device_token query-param 连进来. Hub:
1. 校验 token → 找 app
2. 等 control op=hello, 回 control op=ready
3. 加入 online registry, 后续帧靠这条 WS 流转

M-Hub-0 范围: 只跑通握手 + registry; HTTP/WS forward 在 M-Hub-1 加.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..shared import tunnel_proto as tp
from . import db as hub_db

log = logging.getLogger("ccr.hub.tunnel")

router = APIRouter()


class _OnlineApp:
    """一个 connected app 的运行时状态."""
    __slots__ = ("app_id", "user_id", "name", "ws", "connected_at",
                 "version", "capabilities")

    def __init__(self, app_id: str, user_id: str, name: str,
                 ws: WebSocket, version: str = "",
                 capabilities: list[str] | None = None) -> None:
        self.app_id = app_id
        self.user_id = user_id
        self.name = name
        self.ws = ws
        self.connected_at = time.time()
        self.version = version
        self.capabilities = capabilities or []


class _Registry:
    """全局: app_id → _OnlineApp. 多进程后要改成 redis / 别的."""

    def __init__(self) -> None:
        self._apps: dict[str, _OnlineApp] = {}
        self._lock = asyncio.Lock()

    async def add(self, app: _OnlineApp) -> None:
        async with self._lock:
            # 已有同 app_id 旧连接? 踢它.
            old = self._apps.get(app.app_id)
            if old:
                try:
                    await old.ws.close(code=1012, reason="superseded")
                except Exception:
                    pass
            self._apps[app.app_id] = app

    async def remove(self, app_id: str, ws: WebSocket) -> None:
        async with self._lock:
            cur = self._apps.get(app_id)
            if cur and cur.ws is ws:
                self._apps.pop(app_id, None)

    def get(self, app_id: str) -> _OnlineApp | None:
        return self._apps.get(app_id)

    def online_apps(self) -> list[_OnlineApp]:
        return list(self._apps.values())

    def online_app_ids(self) -> list[str]:
        return list(self._apps.keys())


registry = _Registry()


async def _safe_close(ws: WebSocket, code: int, reason: str = "") -> None:
    if ws.client_state == WebSocketState.DISCONNECTED:
        return
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


@router.websocket("/app-tunnel")
async def app_tunnel(
    ws: WebSocket,
    token: str = Query(...),
):
    """App 端连进来的反向 WS endpoint.

    流程:
      ① accept ws
      ② 校验 token → 找 app 记录
      ③ 等 hello 帧 (5s 超时); 校验 version
      ④ 发 ready 帧, 加入 registry, 标 last_seen
      ⑤ 进入 read loop — 收 ping → 回 pong; 其它帧 M-Hub-0 暂忽略
    """
    await ws.accept()

    row = await hub_db.find_app_by_token(token)
    if not row:
        await _safe_close(ws, code=1008, reason="bad_token")
        return

    # 等 hello (5s)
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=5)
    except asyncio.TimeoutError:
        await _safe_close(ws, code=1008, reason="hello_timeout")
        return
    except WebSocketDisconnect:
        return

    try:
        hello = tp.decode(raw)
    except Exception:
        await _safe_close(ws, code=1003, reason="bad_hello_format")
        return

    if not isinstance(hello, tp.Control) or hello.op != "hello":
        await _safe_close(ws, code=1003, reason="expected_hello")
        return

    version = str(hello.data.get("version", ""))
    if version and version != tp.TUNNEL_PROTO_VERSION:
        await _safe_close(ws, code=1008, reason=f"version_mismatch:{version}")
        return

    online = _OnlineApp(
        app_id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        ws=ws,
        version=version,
        capabilities=list(hello.data.get("capabilities") or []),
    )
    await registry.add(online)
    await hub_db.touch_app_seen(online.app_id)
    log.info("app online: %s (%s) user=%s",
             online.app_id, online.name, online.user_id)

    # ready
    ready = tp.Control(
        stream_id="*",
        op="ready",
        data={"app_id": online.app_id, "user_id": online.user_id},
    )
    await ws.send_text(tp.encode(ready))

    # main loop
    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = tp.decode(raw)
            except Exception:
                log.warning("bad frame from %s: %r", online.app_id, raw[:200])
                continue

            if isinstance(frame, tp.Ping):
                await ws.send_text(tp.encode(tp.Pong(stream_id=frame.stream_id)))
            elif isinstance(frame, tp.Control):
                # M-Hub-0 不处理具体 control op; M-Hub-2 (metadata sync) 来填.
                log.debug("control op=%s ignored (M-Hub-0)", frame.op)
            else:
                # http_res / ws_msg etc — M-Hub-1 才需要分发
                log.debug("frame type=%s ignored (M-Hub-0)",
                          getattr(frame, "type", "?"))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("tunnel loop error for app %s", online.app_id)
    finally:
        await registry.remove(online.app_id, ws)
        log.info("app offline: %s", online.app_id)
