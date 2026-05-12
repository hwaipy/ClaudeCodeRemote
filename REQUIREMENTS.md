# ClaudeCodeRemote 需求与设计交接

> 这份文档是 2026-05-12 在另一个会话里讨论后落下来的交接稿。读它就够了，不用回看旧对话。

## 一、目标体量

**Tier 3：接近 `claude.ai/code` 的体验**。具体落到这些能力：

1. **聊天 UI**：消息流（user / assistant / system / tool_use / tool_result），
   Markdown 渲染，代码块高亮，可滚动历史。
2. **工具调用展示**：Bash 命令、Read / Edit / Write 的目标文件，参数和结果都可展开。
3. **工具批准（permission gate）**：默认拦截敏感工具（Edit/Write/Bash 等），
   UI 上弹批准框，「允许一次 / 始终允许此命令 / 始终允许此工具 / 拒绝」。
4. **文件 diff 视图**：Edit/Write 产生的改动以 unified diff 形式渲染，
   有「在编辑器中打开」之类的跳板（VS Code 协议 URL 即可）。
5. **会话管理**：list / resume / fork / delete。Resume 走 `claude --resume <id>`。
6. **多 agent 状态板**：同时跑多个会话时一眼看见每个的状态（idle / running /
   waiting-for-input / waiting-for-permission）。
7. **推送通知**：claude 等输入或等批准时，Web Push 推到手机。
8. **附件 / 富输入**：可粘贴图片、拖文件进去（Claude 4.x 支持图片输入）。
9. **PWA**：手机主屏可装，离线壳，状态栏色调与 app 风格一致。
10. **多用户 / 鉴权**：至少一个简单的 token / Basic Auth，让仓库可以暴露在公网。

## 二、技术路线

### 核心机制

每个会话 = 一个 `claude` 子进程，以 stream-json 模式运行：

```bash
claude \
  --print \
  --output-format stream-json \
  --input-format stream-json \
  --include-partial-messages \
  [--resume <session_id>] \
  [--allowedTools / --disallowedTools / --permission-mode ...]
```

- **stdout**：每行一个 JSON 事件（`message_start`、`content_block_delta`、
  `tool_use`、`tool_result`、`permission_request`、`message_stop` 等，
  具体字段以实际 CLI 输出为准，先抓一遍样本入库）。
- **stdin**：每行一个 JSON，把用户消息和权限决定喂回去。
- 后端做的就是「转发 + 持久化 + 鉴权」。

### 不要做的事

- ❌ **不要 tmux**。tmux 路线是为了复用终端 TUI，跟我们的目标（结构化事件 + 聊天 UI）相反。
- ❌ **不要解析 TUI 输出 / capture-pane**。脏，没必要。
- ❌ **不要重新发明协议**。Claude CLI 已经吐 stream-json，直接用。

### 技术栈建议（可以推翻）

- **后端**：FastAPI + uvicorn。要管多个长连子进程 + WebSocket，async 是必需的。
  Flask + threading 也能凑合，但 WebSocket 上 FastAPI 顺手得多。
- **WebSocket 协议**：每个会话一条 WS，前后双向都是 JSON。事件类型大致沿用
  Claude CLI 的 stream-json 形态，再加自定义的 `permission_decision`、`session_state` 等。
- **前端**：先用单文件渐进式（Alpine.js / htmx / vanilla JS + 一点 Tailwind CDN），
  避免一上来就上 React/Vite 工具链。等功能稳了再考虑重构成 SPA。
- **持久化**：SQLite 单文件足够。表大概是 `sessions`、`messages`、`permissions`、`users`。
- **进程托管**：systemd user service（参考基线已有的 `claude-launcher.service` 写法）。
- **鉴权**：第一版做 single-user + bearer token（环境变量配置）。多用户后续再说。

## 三、架构草图

```
┌──────── 浏览器/PWA ─────────┐
│  React-less 单页 + WS 客户端 │
└──────────┬──────────────────┘
           │ WebSocket /ws/<session_id>
           │ HTTP /api/sessions, /api/spawn, /api/auth
┌──────────▼──────────────────┐
│  FastAPI (uvicorn, async)   │
│  ┌────────────────────────┐ │
│  │ SessionManager         │ │  ── 维护 {session_id: ClaudeProcess}
│  │  ─ spawn / resume      │ │
│  │  ─ broadcast events    │ │
│  │  ─ permission queue    │ │
│  └──────┬─────────────────┘ │
│         │                   │
│  ┌──────▼─────────────────┐ │
│  │ ClaudeProcess           │ │  ── asyncio.subprocess
│  │  stdin  ◀── user msg    │ │     行级 JSON 双向
│  │  stdout ──▶ events      │ │
│  └─────────────────────────┘ │
│                              │
│  ┌─────────────────────────┐ │
│  │ SQLite (aiosqlite)      │ │
│  └─────────────────────────┘ │
└──────────────────────────────┘
```

## 四、里程碑（建议）

| # | 里程碑 | 范围 | 验收 |
|---|--------|------|------|
| M0 | 协议侦察 | 跑一次 stream-json，把所有事件类型抓下来存成 fixture | 写在 `docs/stream-json-events.md` |
| M1 | 单会话裸聊 | 一个 WS，能 spawn → 发 user msg → 看 assistant 文本流 | 手机能跟 claude 说一句话拿回答 |
| M2 | 工具调用渲染 | tool_use / tool_result 展开 | bash、read、edit 都看得见 |
| M3 | 权限门 | UI 弹批准框，决定写回 stdin | 默认 deny，UI 上点允许才执行 Bash |
| M4 | 会话管理 | list / resume / delete，SQLite 持久化 | 重启服务后能看到/恢复会话 |
| M5 | 多 agent + 通知 | 状态板 + Web Push | 后台 claude 等输入时手机收到推送 |
| M6 | 文件 diff + 附件 | 渲染改动 / 接收图片粘贴 | 改一个文件能在 UI 上看 diff |
| M7 | PWA 抛光 | manifest / SW / 主屏图标 / 离线壳 | 安装后离线打开仍能看到历史 |

> M0 是关键。先把 stream-json 实际事件格式摸清楚，剩下的设计才有根。

## 五、可以从基线偷的东西

`app.py`（基线 Flask 启动器）里这些片段值得抄过来：

- 暗色配色 / 圆角卡片 CSS 风格——直接复用。
- PWA manifest / service worker / 居中 `>_` 图标——已经调好了，搬过来。
- `PRESET_DIRS` 工作目录选择 UX。
- `claude-launcher.service` 这类 systemd user unit 部署模板。
- 路径校验（`NAME_RE`、`resolve_path` 之类）。

## 六、开放问题（新会话决定）

- **进程生存期**：claude 子进程要不要常驻？还是每条消息起一次？
  建议常驻（保留上下文连贯性 + 减少冷启动），但要有空闲超时（如 30 分钟无活动自动 hibernate，
  下次从 `--resume` 复活）。
- **多用户**：第一版要不要支持？目前提议「不」，先 single-user + token。
- **session_id 来源**：用 claude 自己生成的 UUID，还是我们额外包一层？建议直接用 claude 的。
- **运行环境**：跑在 Linux 服务器上，cwd 可选；要不要做容器隔离？第一版不做。

## 七、附：当前基线提供的接口

`app.py` 现有路由（可参考，但下版很可能重做）：

- `GET /` — 单页 HTML
- `GET /api/sessions` — 会话列表（带 tmux 状态判断）
- `POST /api/spawn` — 新建 tmux + claude 会话
- `POST /api/kill | /api/restart | /api/destroy | /api/hide | /api/dismiss`
- `GET /api/ls?path=` — 目录浏览（用于选 cwd）
- `GET /manifest.webmanifest | /sw.js | /icon.svg` — PWA 三件套

会话状态文件：`~/.config/claude-launcher/sessions.json`（基线用，新版可不沿用）。
