# ClaudeCodeRemote

自建的 Claude Code 远程控制台。目标是用 Claude CLI 的 stream-json 协议托管会话，
通过 Web/PWA 提供接近 `claude.ai/code` 的体验——聊天界面、工具批准、文件 diff、
推送通知等——但完全跑在自己机器上。

## 现状

- `app.py` 是从 `Sundries/20260510_ClaudeAutoRemoteSession/app.py` 搬过来的**基线**，
  当前是个 Flask「启动器」：在 tmux 里 spawn `claude`，Web UI 只能列/杀/重启会话，
  点会话只能跳到 `claude.ai/code/<id>`。
- 已经具备 PWA 外壳（manifest / service worker / icon），可装到主屏。
- **这个基线后续会被大改甚至替换**——保留在仓库里是为了参考 UI 风格、API 形态、
  systemd 部署习惯。tmux 路线本身在新设计里会被丢弃。

## 目标

详见 [REQUIREMENTS.md](REQUIREMENTS.md)。

简单说：自托管一个能在手机上真正「跟 Claude 聊天」的 Web 控制台，不再只是
跳到 claude.ai。

## 后续开发约定

新会话进来先读 [CLAUDE.md](CLAUDE.md) 和 [REQUIREMENTS.md](REQUIREMENTS.md)。
