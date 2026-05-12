# Claude CLI stream-json 事件清单（M0 实测）

> 实测自 `claude 2.1.138`，命令：
>
> ```
> claude --print --output-format stream-json --input-format stream-json \
>        --include-partial-messages --include-hook-events --verbose
> ```
>
> Raw 全量数据：[`stream-json-raw.jsonl`](./stream-json-raw.jsonl)（行号在文中以 `L#` 引用）。
> Stderr：[`stream-json-stderr.log`](./stream-json-stderr.log)（若有）。
>
> 采集脚本：[`../scripts/probe_stream_json.py`](../scripts/probe_stream_json.py)。

## 关键发现（设计前必须知道的几件事）

1. **默认 `permissionMode=bypassPermissions` 来自用户 settings.json**（不是 `--print` 的内建默认）。
   `~/.claude/settings.json` 里 `permissions.defaultMode` 决定，传 `--permission-mode <mode>`
   覆盖之。**M3 必须显式 `--permission-mode default` 才有可能拦截**。
2. **`--permission-mode default` 单独不够**，必须配 PreToolUse hook 才会拦。场景 `perm_default`
   只传 mode 不配 hook，Bash 仍直接放行。
3. **运行期权限拦截的官方路径是 PreToolUse hook**（M0.5 实证）。
   `--permission-prompt-tool` 这个隐藏 flag 在 `--print` 模式下被忽略（MCP server 只收到
   `initialize`+`tools/list`，从不收 `tools/call`，场景 `perm_mcp_strict`）。详见
   §权限通道（M0.5 落地）。
4. **`--disallowedTools` / `--allowedTools` 是启动期黑/白名单**，不是运行期拦截。
   场景 `perm_disallowed`：模型 init 时拿到的工具列表里就没有 Bash，自然不会调用
   （L112 assistant text："Bash isn't in my available tools this session"）。
5. **每个事件都带 `session_id` (UUID) + 自身 `uuid`**。session_id 由 CLI 生成，对应
   `~/.claude/projects/<slug>/<session_id>.jsonl`，可用 `--resume <session_id>` 恢复。
6. **高层事件 `assistant` / `user` 是 `stream_event` 的聚合视图**（冗余但好用）：
   - 流式 UI 用 `stream_event/*`（打字效果、partial_json 拼工具参数）。
   - 持久化只存 `assistant` / `user`（拿到完整 message 一次性入库）。
7. **`system.post_turn_summary` 给出 `status_category`**（`review_ready` / `needs_action` 等），
   对应基线 `app.py` 的 idle/busy/needs-input 概念。M5 状态板用这个。

## 输入协议（stdin）

每行一个 JSON。M0 验证过的最小形态：

```json
{"type": "user", "message": {"role": "user", "content": "Hello"}}
```

`content` 也支持数组形式（与 Anthropic SDK 一致）；M0 未单独验证多模态/图片输入，留到 M6。

输入流的其它 message type（如可能存在的 control_request / permission decision）**未在 M0
验证**，见 §开放问题。

## 输出协议（stdout）

每行一个 JSON。顶层 `type` 是分类入口，目前观察到的值：

| `type` | 出现次数 | 作用 |
|---|---|---|
| `system` | 多 | 元数据：init / status / post_turn_summary |
| `rate_limit_event` | 1（开头） | 速率限制状态 |
| `stream_event` | 多 | 实时 SSE 风格事件（嵌套 `event.type`） |
| `assistant` | N | 一轮 LLM 输出的完整 message（聚合视图） |
| `user` | N | tool_result 回喂（聚合视图） |
| `result` | 1（结尾） | 整轮成本 / 终止原因 |

下文按 type 展开。

---

### `system`

三种 subtype：

#### `system.init`（L2）— 会话握手

关键字段：
- `cwd`：实际工作目录
- `session_id`：UUID，用于 `--resume`
- `tools`：本次会话**可用的工具名列表**（受 `--allowed/disallowedTools` 影响）
- `mcp_servers`：MCP 列表
- `model`：实际模型 ID（如 `claude-opus-4-7[1m]`）
- `permissionMode`：本次实际生效的 permission mode
- `slash_commands` / `agents` / `skills` / `plugins`：可用功能盘点
- `claude_code_version`
- `memory_paths.auto`：auto-memory 路径

设计含义：M1 一启动就发这条，前端从此知道 session_id、cwd、可用工具集合。

#### `system.status`（L3, L18, L31...）— 进度提示

字段：`status`（如 `requesting`）。在请求模型前、每轮 tool 调用之间出现。
M5 状态板可拿这个驱动 UI 状态机。

#### `system.post_turn_summary`（L13, L76, L102）— 一轮结束的总结

字段：
- `summarizes_uuid`：所总结的 assistant message 的 uuid
- `status_category`：例如 `review_ready`
- `status_detail`：人类可读摘要（如 `"pong"`、`"done"`）
- `needs_action`：空 / 待动作描述

设计含义：UI 的「等输入」「等批准」状态来源；M5 推送通知判定依据。

---

### `rate_limit_event`（L4）

每会话开头一条，字段 `rate_limit_info`：

- `status`：`allowed` / 其它
- `resetsAt`：unix 秒
- `rateLimitType`：`five_hour` / 其它
- `overageStatus` / `overageDisabledReason` / `isUsingOverage`

设计含义：M1 可以在前端角落显示「还剩多少」，超额时提前警告。

---

### `stream_event`（流式 SSE 镜像）

嵌套 `event.type`，是 Anthropic Messages API streaming 事件原样转发。观察到的 subtype：

#### `message_start`（L5, L20, L32, ...）

`event.message` 是 partial assistant message 骨架（含 model / id / 初始 usage）。
顶层还有 `ttft_ms`（首 token 时延，仅首条 message）。

#### `content_block_start`（L6, L21, L33, ...）

`event.content_block.type` 是 `text` 或 `tool_use`：

- `text`：`{type: "text", text: ""}` 占位
- `tool_use`：`{type: "tool_use", id: "toolu_xxx", name: "Write", input: {}}`，**input 是空的，靠后续 delta 拼**

#### `content_block_delta`（L7, L8, L22-25, ...）

两种 `delta.type`：

- `text_delta`：`{type: "text_delta", text: "p"}` — 文本流（打字效果）
- `input_json_delta`：`{type: "input_json_delta", partial_json: "..."}` — **工具参数 JSON 是分片
  流出的**。后端要按 `index` 累加 `partial_json`，最后 parse 才能拿到完整 input。

设计含义：M2 渲染工具参数有两条路：等 `assistant` 事件拿完整 input（简单）；
或边收边拼（适合长 input 的渐进展示，比如长 Edit）。

#### `content_block_stop` / `message_delta` / `message_stop`

结束标记。`message_delta` 含 `delta.stop_reason`（`tool_use` / `end_turn`）+ cumulative `usage`。

---

### `assistant`（L9, L26, L37, ...）

完整 assistant message 的聚合视图，**在 `message_stop` 之前发出**（实测 L9 < L12）。

`message.content` 是数组，元素 `type`：
- `text`：`{type: "text", text: "pong"}`
- `tool_use`：`{type: "tool_use", id, name, input}` — **input 已是完整 dict**

附加字段：`parent_tool_use_id`（None 或 toolu_xxx）、`uuid`、`session_id`。

设计含义：**持久化只看 assistant 就够了**。流式 UI 只用 stream_event。

---

### `user`（L29, L40, L53, L67, ...）

工具结果回喂，聚合视图：

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {
        "type": "tool_result",
        "tool_use_id": "toolu_xxx",
        "content": "...输出...",
        "is_error": false
      }
    ]
  }
}
```

`content`（tool_result 的 content）可以是字符串，也可能是数组（多模态结果，如图片）——后者 M0 未触发。

`is_error: true` 时 result string 通常包含错误描述。

设计含义：M2 渲染工具结果时按 tool_use_id 跟 assistant 里的 tool_use 配对。

---

### `result`（L14, L77, L103；每会话最末）

整轮收尾。关键字段：
- `subtype`：`success` / 其它
- `is_error`：bool
- `duration_ms` / `duration_api_ms`：墙钟 / API 时间
- `num_turns`：本轮一共多少 LLM 调用
- `result`：assistant 最后一条文本（便利字段）
- `stop_reason`：`end_turn` / `tool_use` / ...
- `total_cost_usd`：本轮总花费
- `usage`：聚合的 token / cache / web 搜索
- `modelUsage`：按模型分账（看到一次混用 opus-4-7 + haiku-4-5）
- `permission_denials`：deny 数组（M0 全为空，待 M3 验证）
- `terminal_reason`：`completed` / 其它

设计含义：M4 持久化「消息累计成本」就读这个；M5 状态板「会话已结束 vs 仍活」也看这。

---

## 权限通道（M0.5 落地）

### 走通的路：PreToolUse hook

**结论先行**：M3 走 **PreToolUse hook**，命令同步阻塞等后端决定。MCP `permission_prompt_tool`
路径在 `--print` 模式下不可用，放弃。

#### 启动配置

`--permission-mode default` + `--settings <file>` 指向一份 settings JSON，含：

```json
{
  "permissions": {"defaultMode": "default"},
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{"type": "command", "command": "/abs/path/to/hook_bridge"}]
    }]
  }
}
```

`matcher` 是正则 / 工具名通配（实测 `"Bash"` 命中 Bash；用 `".*"` 应该能拦所有）。

#### Hook 命令的 IO 合约

**stdin**：claude 写一份 JSON（实测见 `docs/hook-calls.jsonl`）：

```json
{
  "session_id": "06b9bd5f-...",
  "transcript_path": "/home/.../<sid>.jsonl",
  "cwd": "<process cwd>",
  "permission_mode": "default",
  "effort": {"level": "xhigh"},
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {"command": "date", "description": "..."},
  "tool_use_id": "toolu_01..."
}
```

附带的环境变量：`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`,
`CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_AGENT_SDK_VERSION`, 等。

**stdout**：JSON 决定（exit 0 即可）：

- 允许：
  ```json
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "allow",
                          "permissionDecisionReason": "<可选 reason>"}}
  ```
- 拒绝：
  ```json
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "deny",
                          "permissionDecisionReason": "<显示给模型的原因>"}}
  ```

#### Stream 上的副作用

- **`system/hook_started`**（新事件类型，由 `--include-hook-events` 启用）：
  `{hook_id, hook_name: "PreToolUse:Bash", hook_event: "PreToolUse", session_id}`
- **`system/hook_response`**：同上 + `output / stdout / stderr / exit_code / outcome`。
  整条 stdout 字符串原样含在 `output` 字段——后端可以从这判断 hook 实际返回了什么。
- **Deny 路径**：`tool_result.is_error=true`，`content` 就是 `permissionDecisionReason`；
  模型看到 deny 后正常继续推理。`result.permission_denials` 数组末尾追加
  `{tool_name, tool_use_id, tool_input}`。
- **Allow 路径**：跟正常工具调用一样，无额外标记，差别只是中间多了两条 hook 事件。

事件序列实测（hook_deny 场景 L177-205，hook_allow L206-234）：

```
message_start
content_block_start(tool_use=Bash)
content_block_delta × N   # 流出 tool input
assistant(tool_use)
system/hook_started        ← hook 执行中
content_block_stop
message_delta(stop=tool_use)
message_stop
system/hook_response       ← hook 返回
user(tool_result)          ← is_error 取决于 hook 决定
…（下一轮 message_start，模型继续）
```

#### M3 设计映射

| 前端按钮 | hook 输出 + 后端记忆 |
|---|---|
| 允许一次 | `{permissionDecision: "allow"}` |
| 始终允许此工具 | 后端先写白名单 → 下次 hook 命中前缀匹配直接 allow，不弹 UI |
| 始终允许此命令 | 后端按 `tool_name + tool_input` hash 精确匹配，同上 |
| 拒绝 | `{permissionDecision: "deny", permissionDecisionReason: "user denied"}` |

桥接器（`hook_bridge`）的工作：从 stdin 读 payload → 通过 unix socket / HTTP 把请求送给主
后端 → 等后端决定（前端 WS push + 用户点击）→ 输出 JSON 决定 → 退出。

### 走不通的路：MCP `permission_prompt_tool`

`--permission-prompt-tool mcp__ccr__permission_prompt` 是隐藏 flag（`--help` 不列）。设置后
MCP server 起进程、初始化、列工具都正常（init 报告 `mcp_servers: [{name: "ccr", status: "connected"}]`），
但工具的 `tools/call` 从不被触发——Bash 直接放行。

可能原因（未验证，先记下不再追究）：此 flag 设计给 SDK 的非 `--print` 模式或别的入口，
CLI 的 `--print` 跑这条路径时不调用 MCP 权限工具。

走 hook 已经够用，不再回头。

---

## 已覆盖的场景

| Scenario | 行号 | 说明 |
|---|---|---|
| `plain_chat` | L1-15 | 纯文本回复，无工具 |
| `tools_basic` | L16-78 | Write → Read → Edit → Bash 四工具串行 |
| `perm_default` | L79-104 | `--permission-mode default` 不配 hook —— **直接放行** |
| `perm_disallowed` | L105-121 | `--disallowedTools Bash` —— 启动期黑名单 |
| `perm_mcp_allow` | L122-148 | MCP permission_prompt_tool（`--setting-sources=`） —— **MCP 不被调用，Bash 放行** |
| `perm_mcp_strict` | L149-176 | MCP path 第二次确认 —— 仍不调 MCP |
| `hook_deny` | L177-205 | PreToolUse hook 输出 deny —— **拦截成功**，is_error=true，permission_denials 填充 |
| `hook_allow` | L206-234 | PreToolUse hook 输出 allow —— Bash 正常执行 |

## 开放问题（影响后续设计）

- [x] ~~**运行期权限拦截的真正通道**~~ → M0.5 落地：走 PreToolUse hook，详见 §权限通道。
- [x] ~~**`--include-hook-events` 触发的钩子事件**~~ → M0.5 落地：`system/hook_started`、
  `system/hook_response`。
- [ ] **`assistant` 事件相对 `stream_event/message_stop` 的精确顺序**：实测显示 `assistant`
  在 `content_block_stop` 之前就发了（L9 vs L10）——有点反直觉，后端解析时不能假设 message
  完整=stream 结束。
- [ ] **图片 / 附件输入**：stdin user message 里 `content` 是数组时的 image block 形态，留到 M6。
- [ ] **长会话 / 多轮 `--resume`**：恢复后是否会重发 `system.init`？session_id 是否变化？
  M4 开工前抓一遍。
- [ ] **`parent_tool_use_id` 非 None 的情形**：本轮全为 None，应该是 Task agent / subagent 调
  用嵌套时才有。M2 / M5 设计 agent 状态板时再补抓。
- [ ] **rate-limit 触发时的事件形态**：`rate_limit_event.status="allowed"`，没看到限流真实发
  生时的样子。
- [ ] **错误路径**：API 出错 / 模型拒答 / Bash 命令失败时的事件差异。tool_result `is_error=true`
  下的 content 形态除 hook deny 外没采集到。

## 设计决策落地（写给 M1+）

1. **后端 ↔ 子进程**：行级 JSON 双工。stdin 喂 `{type:"user", message:...}`；stdout 收所
   有 type，按上文分发。
2. **后端 ↔ 前端 WS 协议**：直接转发 stream-json 顶层事件（多包一层 envelope，加 `ts` 和
   会话事件序号），不重新发明。
3. **持久化最小集合**：只存 `assistant` / `user` / `system.init` / `result`；`stream_event`
   不入库（实时性事件，丢了无所谓，回放从 `assistant`/`user` 重建即可）。
4. **状态机驱动**：
   - 收到 `system.init` → state = `running`
   - 收到 `system.status` → 取 `status` 字段映射
   - 收到 `system.post_turn_summary` → state = `idle` 或 `needs_action`
   - 收到 `result` → state = `done`
5. **权限门（M3）落地路径**（M0.5 spike 后定稿）：起进程时传
   `--permission-mode default` + `--settings <file>`；settings 里挂 PreToolUse hook 指向桥接器
   命令。桥接器从 stdin 拿 payload，转给后端等待用户决定，按 hook 协议输出 JSON。具体 IO 合约见
   §权限通道（M0.5 落地）。
