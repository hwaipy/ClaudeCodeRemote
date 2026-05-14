# 给 Claude 的项目说明

## 你在哪儿

`~/codes/ClaudeCodeRemote`。这是一个**自建 Claude Code 远程控制台**项目，
目标是用 Claude CLI 的 stream-json 协议托管会话，做一个跑在自己机器上、能从手机
PWA 跟 Claude 聊天 + 批准工具 + 看 diff 的 Web 控制台。

## 先读这两份

1. `REQUIREMENTS.md` — 需求、技术路线、里程碑。
2. `README.md` — 一句话概览。

`app.py` 是**基线**（Flask + tmux 启动器），保留作参考。新设计**不要 tmux、
不要 capture-pane**，走 stream-json 子进程路线。

## 工作模式

- 推荐从 **M0（协议侦察）** 开始：跑一次 `claude --print --output-format stream-json
  --input-format stream-json`，喂一条消息，把所有 stdout 事件抓下来存到
  `docs/stream-json-events.md` 当 fixture。所有后续设计都以实测事件格式为准。
- 不要凭训练记忆假设事件字段——CLI 在迭代，**抓一遍**再说。

## 不要做的

- 不要解析终端 TUI 输出。
- 不要 tmux。
- 不要一上来就上 React/Vite。先 vanilla / Alpine / htmx 把流程跑通。
- 不要无视基线里的 CSS 风格——配色和组件抄过来就行。

## 部署习惯

跑在 Linux，用 systemd user service。基线的 `claude-launcher.service` 是参考模板
（在用户的 `~/.config/systemd/user/` 下，本项目不带）。

## 用户偏好

- 中文交流。
- 简洁、动作快、少废话。
- 提议大改前先确认。

## Spec-first 工作流（强制）

任何用户要求的改动，**先改 spec，再动手改代码**。

- spec 在 `tests/` 下的测试文件里：`test_spec_only.py` 收待办，已实现的拆到对应
  `test_*.py`。同时 `SPEC.html`（archive 性质）保留可视化效果图。
- 改完 spec 后，**必须保证要求的测试是绿的**才算完成（实现 + 测试同步）。
- **简单修改**（单文件改动、行为局部、不改数据模型）：改完 spec 立即动手，
  跑一遍测试确认绿。
- **复杂修改**（跨文件、新增 schema、改接口语义、影响多个用户视图）：
  改完 spec 先发给用户确认方案，再动手。

判断"简单/复杂"看不准时按复杂处理。
