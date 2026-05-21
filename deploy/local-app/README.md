# Local Docker App — claude code + ccr-app → hub

本机 docker 起一个独立的 ccr-app, 内含 claude code CLI, 反向连接 hub.qpqi.group
成为第二个 app. UbuntuClaw (systemd ccr-autotest) 跟 ClawDocker (这个 container)
在 hub 上合并展示.

## 1. 装 Docker

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
newgrp docker     # 或者重新登录
```

## 2. 在 hub UI 拿 pairing code

浏览器进 https://hub.qpqi.group → 登录 → DevTools 控制台:
```js
await fetch('/api/hub/pair', {method:'POST'}).then(r=>r.json())
```
拿到 `{"code":"NNNNNN-xxxxxxxx", ...}`. 然后在本机:

```bash
curl -X POST https://hub.qpqi.group/api/hub/pair/redeem \
  -H 'Content-Type: application/json' \
  -d '{"code":"NNNNNN-xxxxxxxx","app_name":"ClawDocker"}'
# → {"device_token":"tok-...","app_id":"app-..."}
```

## 3. 起 container

```bash
cd deploy/local-app
cat > .env <<EOF
CCR_TOKEN=$(openssl rand -hex 16)
CCR_HUB_URL=wss://hub.qpqi.group
CCR_HUB_DEVICE_TOKEN=tok-xxx        # 上一步 redeem 拿的
CCR_HUB_APP_NAME=ClawDocker
ANTHROPIC_API_KEY=                  # 留空就是 mock; 真跑填 sk-ant-...
EOF
docker compose up -d --build
docker compose logs -f ccr-app-local
```

## 4. 验证

- 浏览器 https://hub.qpqi.group → 应看到两个 app online: UbuntuClaw + ClawDocker
- New session modal 的 App 下拉可以选 ClawDocker, spawn 出来的 session 在
  容器里跑 claude code

## 5. 进容器调试

```bash
docker exec -it ccr-app-local bash
# 容器内可以直接跑 claude:
claude --version
claude "hello"      # 需要 ANTHROPIC_API_KEY
```

## 6. 卸载

```bash
docker compose down -v   # -v 顺便清掉 ./data + ./workspace
```
