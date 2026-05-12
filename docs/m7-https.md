# M7 后续：HTTPS + Web Push 路径

> 本文档不在本里程碑实施，只整理可行路径供后续选择。

## 为什么需要 HTTPS

只有在 **secure context**（https / localhost）下浏览器才会：

- 允许注册 Service Worker（PWA 真正可装/离线）
- 允许 `PushManager` 订阅 → OS 级锁屏通知
- 允许 `navigator.clipboard` 写入、`crypto.subtle` 等 API
- iOS Safari **必须 https** 才能「添加到主屏」

公网现在走 `http://code.qpqi.group:1882`，SW 静默注册失败、不能 OS push。

## 三条可走的路

### A. 云端 nginx 反代到 `claude.hwaipy.cn/ccr/`

`claude.hwaipy.cn` 已在 `101.34.241.89` 上跑 nginx 1.29.4（已配 https 给
`/files/synology/` 用）。让那台 nginx 加：

```nginx
location /ccr/ {
    proxy_pass http://code.qpqi.group:1882/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
}
```

挑战：

1. **路径前缀 `/ccr/`** 让前端所有绝对路径（`/api/...`、`/ws/...`、
   `/static/...`、`/manifest.webmanifest`、`/sw.js`）都得改成相对路径或
   读 `<base href>`。SW 还要重新算 scope。需要前端改造。
2. SW 的 scope 默认是脚本所在路径，注册到 `/ccr/` 即可，但响应头里
   `Service-Worker-Allowed: /` 不能用了（不能跨 path 提升 scope）。
3. WebSocket 通过 nginx 反代需要 `Upgrade` 头（如上配置已加）。

建议改造范围：让 server 接受 `root_path` 参数（uvicorn `--root-path /ccr`），
FastAPI 内部路由不动；index.html 模板把 `__BUILD_ID__` 旁边再注入一个
`__ROOT__`，前端 fetch / WS 用 `__ROOT__` 拼前缀。

### B. 给 `code.qpqi.group:1882` 配 TLS（frp 那一端启 wss）

frp 支持在 server 端做 TLS termination。配 `tls_enable = true` + 证书。
但 `1882` 是 tcp 透传，**不是 http**——frp 本身不做证书。需要在 frp 后面
再放一个 nginx/Caddy 终止 TLS，反代到本机 1881。云端服务器上需要权限。

### C. 本机直接监听 443 + 自签证书（仅局域网用）

`uvicorn ... --ssl-keyfile xxx --ssl-certfile xxx --port 443`。

只在受信任的局域网内有效，公网仍然需要 A 或 B。

## 推荐

近期：**方案 A**（云端 nginx 加 location），最小代价拿到 https + SW + Push。
但需要前端 `root_path` 改造（中等工作量）+ 云端 nginx 改 conf。

## Web Push 准备（HTTPS 落地后）

1. 后端：依赖 `cryptography`（已有），手写或拉 `pywebpush`。生成
   VAPID 密钥对持久化（`~/.config/ccr/vapid.{key,pub}`）。
2. 新 endpoint：
   - `POST /api/push/subscribe`：保存浏览器 subscription（endpoint + p256dh + auth）到 sess 或全局
   - `POST /api/push/test`：调试用
3. 触发点：`SessionManager._apply_state_signals` 检测到状态变 waiting_permission /
   needs_input 时入推送队列；后端 worker 拿订阅 + payload 调
   Web Push 端点
4. 前端 SW 加 `push` 监听 → `showNotification`；点通知打开 `/ccr/?sess=<id>`
