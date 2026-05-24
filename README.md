# Vibing everything

自托管的 Claude Code 远程控制台 (项目仓库名 ClaudeCodeRemote / CCR, 品牌
"Vibing everything"). 用 Claude CLI 的 stream-json 协议托管会话, 通过
Web/PWA 提供完整的 `claude.ai/code` 体验 — 聊天、工具批准、diff、推送, 跨
设备同步, 全部跑在自己机器上.

## 现状 (2026-05)

可用功能:

- **多机 Hub 聚合**: 任意台机器跑 CCR server, 反向 WS 接到一个 Hub, 一个
  PWA 面板看所有机器上的 session, 跨机切换无感知
- **完整聊天 UI**: 消息流 + Markdown / 代码高亮 + 工具调用卡 + diff 渲染
  + 权限审批卡 + 自定义 askuser 问答卡
- **PWA**: 加到主屏当 app 用; 自适应 iPhone 刘海; 边缘右滑接管系统手势
- **OAuth 登录**: Google / GitHub / Gitee (已配); Feishu / DingTalk (代码
  现成, 配 client id 即用)
- **多 LLM 后端**: 同一台 CCR server 可对接 Anthropic / DeepSeek / Kimi /
  Qwen 等 (通过 ANTHROPIC_BASE_URL + 各家 OpenAI-compatible 网关)
- **自定义 MCP ask_user**: 绕过 SDK builtin AskUserQuestion 的硬编码极短
  timeout, 用户答题想多久就多久 (见 `claude_code_remote/mcp/`)

## 架构

```
PWA (vibe.qpqi.group)
    ↓ HTTPS + WSS
Hub (FastAPI, 聚合 + auth + forward)
    ↓ 反向 WS tunnel (servers 主动接出)
CCR server × N (每台机器一份, 跑 claude CLI subprocess)
    ↓ stdio stream-json
claude CLI (Anthropic / 第三方 LLM 网关)
```

## 部署

- 自己机器跑 server: 见 PWA Help 页 (登录后顶栏齿轮旁) 的 quick path
- Hub 容器化: 见 `deploy/docker-compose.hub.yml`
- 单机本地 (无 Hub): 见 `deploy/README.md`

## 开发约定

新会话进来先读 [CLAUDE.md](CLAUDE.md) (项目说明 + spec-first 工作流) 和
[REQUIREMENTS.md](REQUIREMENTS.md) (2026-05-12 设计稿, 目标 + 技术路线
仍然准确).

可视化 SPEC 在 `~/SynologyDrive/Claude/ccr-spec.html` (本地路径, 用户私有
工作文件; 每次 UI 改动应同步 SVG mockup + 行为表).

## 仓库

源: <https://github.com/hwaipy/ClaudeCodeRemote>

`app.py` 是 2026-05 前的 Flask + tmux 启动器基线, **已不使用**, 保留作
参考 (UI 配色 / systemd 习惯), 后续会清掉.
