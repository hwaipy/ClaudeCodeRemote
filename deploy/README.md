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
