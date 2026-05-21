"""App 端: 反向 WS client (ccr-hub-spec.html §6).

启动时连 Hub, 维持长连. 收到 http_req 帧 → ASGI in-process 调本进程 FastAPI app
→ 回 http_res. 断线指数退避重连. 没配 hub_url 时整个模块 noop, 纯本地零开销.

约定 env:
  CCR_HUB_URL          e.g. ws://hub.example.com  (空 → 不连)
  CCR_HUB_DEVICE_TOKEN 配对后由 ccr pair 写
  CCR_HUB_APP_NAME     display name (默认 hostname)

App 启动时调 hub_client.start(app, env). 不 block, 后台 task 跑.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket

import websockets

from ..shared import tunnel_proto as tp

log = logging.getLogger("ccr.hub_client")


def _ws_url(base: str) -> str:
    """http://x → ws://x/app-tunnel. ws://x → ws://x/app-tunnel."""
    base = base.rstrip("/")
    if base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    return base + "/app-tunnel"


class HubClient:
    """单实例. start() 后后台跑 connect loop, 直到 stop()."""

    def __init__(self, asgi_app, hub_url: str, device_token: str,
                 app_name: str) -> None:
        self.asgi_app = asgi_app
        self.hub_url = hub_url
        self.device_token = device_token
        self.app_name = app_name
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self.app_id: str | None = None
        self.user_id: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="hub_client")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 1.0   # 成功一轮后重置退避
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("hub_client disconnected: %s; retry in %.1fs",
                         e, backoff)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 1.7, 30.0)

    async def _connect_once(self) -> None:
        url = _ws_url(self.hub_url) + f"?token={self.device_token}"
        log.info("hub_client connecting %s", url)
        async with websockets.connect(url, ping_interval=20,
                                       ping_timeout=15) as ws:
            # hello
            hello = tp.Control(
                stream_id="*",
                op="hello",
                data={
                    "app_name": self.app_name,
                    "version": tp.TUNNEL_PROTO_VERSION,
                    "capabilities": [],
                },
            )
            await ws.send(tp.encode(hello))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            ready = tp.decode(raw)
            if not isinstance(ready, tp.Control) or ready.op != "ready":
                raise RuntimeError(f"unexpected ready frame: {ready!r}")
            self.app_id = ready.data.get("app_id")
            self.user_id = ready.data.get("user_id")
            self.connected.set()
            log.info("hub_client ready app_id=%s user_id=%s",
                     self.app_id, self.user_id)

            try:
                await self._loop(ws)
            finally:
                self.connected.clear()
                self.app_id = None
                self.user_id = None

    async def _loop(self, ws) -> None:
        send_lock = asyncio.Lock()

        async def send(frame: tp.AnyFrame) -> None:
            async with send_lock:
                await ws.send(tp.encode(frame))

        async def handle_http_req(req: tp.HttpReq) -> None:
            try:
                res = await self._dispatch_http(req)
            except Exception as e:  # noqa: BLE001
                log.exception("dispatch http_req failed: %s", e)
                res = tp.HttpRes(
                    stream_id=req.stream_id, status=500,
                    body_b64=base64.b64encode(
                        f"app dispatch error: {e}".encode()
                    ).decode("ascii"),
                )
            await send(res)

        async for raw in ws:
            try:
                frame = tp.decode(raw)
            except Exception:
                log.warning("bad frame: %r", raw[:200] if isinstance(raw, str) else raw)
                continue
            if isinstance(frame, tp.Ping):
                await send(tp.Pong(stream_id=frame.stream_id))
            elif isinstance(frame, tp.HttpReq):
                asyncio.create_task(handle_http_req(frame))
            elif isinstance(frame, tp.Control):
                log.debug("control op=%s (server -> app) ignored", frame.op)
            else:
                log.debug("frame %s unhandled",
                          getattr(frame, "type", "?"))

    # ---------- ASGI in-process dispatch ----------

    async def _dispatch_http(self, req: tp.HttpReq) -> tp.HttpRes:
        """构造 ASGI HTTP scope, 调本进程 FastAPI app, 收 response."""
        body = base64.b64decode(req.body_b64) if req.body_b64 else b""

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": req.method,
            "scheme": "http",
            "path": req.path,
            "raw_path": req.path.encode("utf-8"),
            "query_string": req.query.encode("utf-8"),
            "root_path": "",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1"))
                for k, v in req.headers.items()
            ],
            "client": ("hub", 0),
            "server": ("app", 0),
            # via_hub 标记 — app 端 auth 看到 → 跳过 bearer 验证, 信 Hub auth.
            # 外部直连进来的 HTTP request 由 ASGI server 创建 scope, 不存在
            # 这个 key, 因此不可被伪造.
            "state": {"via_hub": True, "hub_user_id": req.user_id or ""},
        }

        body_sent = False

        async def receive() -> dict:
            nonlocal body_sent
            if body_sent:
                return {"type": "http.disconnect"}
            body_sent = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        status: int = 500
        headers: list[tuple[bytes, bytes]] = []
        body_chunks: list[bytes] = []

        async def send(message: dict) -> None:
            nonlocal status
            t = message.get("type")
            if t == "http.response.start":
                status = int(message.get("status", 500))
                headers.extend(message.get("headers") or [])
            elif t == "http.response.body":
                b = message.get("body") or b""
                if b:
                    body_chunks.append(b)
            # http.response.trailers 暂不处理

        await self.asgi_app(scope, receive, send)

        out_headers: dict[str, str] = {}
        for hk, hv in headers:
            try:
                out_headers[hk.decode("latin-1")] = hv.decode("latin-1")
            except Exception:
                pass

        merged = b"".join(body_chunks)
        return tp.HttpRes(
            stream_id=req.stream_id,
            status=status,
            headers=out_headers,
            body_b64=base64.b64encode(merged).decode("ascii"),
        )


def maybe_start(asgi_app) -> HubClient | None:
    """读 env, 决定是否启动 HubClient. 没配 hub_url 时返回 None."""
    hub_url = os.environ.get("CCR_HUB_URL", "").strip()
    token = os.environ.get("CCR_HUB_DEVICE_TOKEN", "").strip()
    if not hub_url or not token:
        log.info("hub_client not started (CCR_HUB_URL or token empty)")
        return None
    name = os.environ.get("CCR_HUB_APP_NAME") or socket.gethostname()
    client = HubClient(asgi_app, hub_url=hub_url, device_token=token,
                       app_name=name)
    client.start()
    log.info("hub_client launched url=%s name=%s", hub_url, name)
    return client
