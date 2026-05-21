# 部署到 systemd user service

## 一次性设置

```bash
# 1. 准备 token
mkdir -p ~/.config/ccr
cp deploy/env.example ~/.config/ccr/env
# 然后编辑 ~/.config/ccr/env，把 CCR_TOKEN 改成真值（建议 openssl rand -hex 24）

# 2. 安装 unit
mkdir -p ~/.config/systemd/user
cp deploy/ccr.service.example ~/.config/systemd/user/ccr.service
# 如果项目目录不是 ~/codes/ClaudeCodeRemote 改一下 WorkingDirectory

# 3. 启用并启动
systemctl --user daemon-reload
systemctl --user enable --now ccr.service
```

## 常用命令

```bash
systemctl --user status ccr
systemctl --user restart ccr
journalctl --user -u ccr -f          # 跟踪日志
systemctl --user disable --now ccr   # 停止 + 关掉开机自启
```

## 让 user service 在没人登录时也运行

```bash
sudo loginctl enable-linger $USER
```

## 反代到子路径（如 https://your.domain/remote/）

把 server 挂到反代域名的子路径下，需要两步：

**1. server 端**：`~/.config/ccr/env` 里加：
```
CCR_ROOT_PATH=/remote
```
这会让 HTML 用 `<base href="/remote/">`，所有资源/API/WS 自动带前缀。

**2. nginx 端**（strip 前缀模式，**推荐**）：

```nginx
# 在 https 的 server 块里
location /remote/ {
    proxy_pass http://192.168.122.98:1881/;   # 末尾 / 让 nginx 把 /remote/ 前缀 strip 掉
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;             # 让流式回复立刻送出
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
}
```

注意：

- `proxy_pass` 末尾的 `/` 不能少，否则 nginx 会**保留** `/remote/` 前缀，本机 server 看到 `/remote/api/sessions` 会 404
- WebSocket 需要 `Upgrade` / `Connection` 头
- `proxy_buffering off` 让 stream-json 实时事件不被 nginx 攒批

访问：`https://your.domain/remote/`，token 用 `Authorization: Bearer <CCR_TOKEN>` 走子路径下的 API。

---

# Hub 部署 (Centralized fan-in)

Hub 是可选的"中心服务" — 装在公网 VPS, 多台 App 反向连上来, 用户浏览器经 Hub
看到所有 apps 的合并 session list (一份 SPA 同时跑 hub + local 模式).

## 1. 起 Hub server

```bash
# 1.1 准备 hub env (admin 凭据 + db 路径)
mkdir -p ~/.config/ccr-hub ~/.local/share/ccr-hub
cp deploy/hub-env.example ~/.config/ccr-hub/env
# 编辑 ~/.config/ccr-hub/env: 填 CCR_HUB_ADMIN_EMAIL / CCR_HUB_ADMIN_PW

# 1.2 装 systemd unit
mkdir -p ~/.config/systemd/user
cp deploy/ccr-hub.service.example ~/.config/systemd/user/ccr-hub.service
systemctl --user daemon-reload
systemctl --user enable --now ccr-hub.service

# 1.3 没人登录时也跑
sudo loginctl enable-linger $USER
```

## 2. nginx TLS 反代

参照 `deploy/nginx-hub.conf.example`. 关键: WS Upgrade 头 + `proxy_buffering off`
+ 1 天读超时 (反向 tunnel 长连). 用 certbot 签 hub.example.com 证书.

## 3. 配对 App → Hub

```bash
# 3.1 浏览器登录 hub UI (https://hub.example.com), 用 admin 凭据
# 3.2 调 POST /api/hub/pair (或后续做 UI 按钮) 拿 pairing code
curl -X POST https://hub.example.com/api/hub/pair \
    -b "ccr_sess=$YOUR_COOKIE"
# → {"code":"123456-deadbeef","expires_at":...}

# 3.3 在 App 机上 redeem 拿 device_token
curl -X POST https://hub.example.com/api/hub/pair/redeem \
    -H 'Content-Type: application/json' \
    -d '{"code":"123456-deadbeef","app_name":"home-mac"}'
# → {"device_token":"tok-...","app_id":"app-...","user_id":"user-..."}

# 3.4 把 device_token 写进 app env (~/.config/ccr/env), restart app
cat >> ~/.config/ccr/env <<EOF
CCR_HUB_URL=wss://hub.example.com
CCR_HUB_DEVICE_TOKEN=tok-xxxxx
CCR_HUB_APP_NAME=home-mac
EOF
systemctl --user restart ccr.service
```

App restart 后会自动连 Hub. 浏览器刷一下 hub.example.com 就能看到该 app online,
sessions list 自动合并.

## 4. Revoke 一个 app

浏览器 hub UI → Settings → Apps → 点对应 app 的 ✕ (或调 DELETE
`/api/hub/apps/<app_id>`). App 端反向 WS 被踢, 不再可连. 重新 pair 即可恢复.

## 多 App 同时连一个 Hub

同样流程跑 N 次 (每台 App 一次 pair + redeem), 各得独立的 device_token. Hub
的 sessions_cache 会合并展示, 每条 session 带 `app_id` / `app_name` /
`app_online` 字段, SPA 卡片上显 app chip.

