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
