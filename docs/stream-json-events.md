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

1. **`--print` 模式下 `permissionMode` 默认是 `bypassPermissions`**（L2 `system.init`）。
   也就是说不显式传 `--permission-mode`，所有工具直接放行——M3 必须显式传别的。
2. **`--print` + `default` permission-mode 不会产生权限事件**。在 stream 上既没收到权限请求，也没人拦
   工具，Bash 直接执行了（场景 perm_default，L93 tool_result `is_error=false`）。
   结论：**运行期权限拦截不能靠 `--permission-mode`，要走 MCP `permission_prompt_tool` 通道
   或 stream-json control protocol**。M3 开工前需要一次专门 spike 验证此路径。
3. **`--disallowedTools` / `--allowedTools` 是启动期黑/白名单**，不是运行期拦截。
   场景 `perm_disallowed`：模型 init 时拿到的工具列表里就没有 Bash，自然不会调用
   （L112 assistant text："Bash isn't in my available tools this session"）。
4. **每个事件都带 `session_id` (UUID) + 自身 `uuid`**。session_id 由 CLI 生成，对应
   `~/.claude/projects/<slug>/<session_id>.jsonl`，可用 `--resume <session_id>` 恢复。
5. **高层事件 `assistant` / `user` 是 `stream_event` 的聚合视图**（冗余但好用）：
   - 流式 UI 用 `stream_event/*`（打字效果、partial_json 拼工具参数）。
   - 持久化只存 `assistant` / `user`（拿到完整 message 一次性入库）。
6. **`system.post_turn_summary` 给出 `status_category`**（`review_ready` / `needs_action` 等），
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

## 已覆盖的场景

| Scenario | 起始行 | 说明 |
|---|---|---|
| `plain_chat` (L1-15) | 纯文本回复，无工具 |
| `tools_basic` (L16-78) | Write → Read → Edit → Bash 四工具串行 |
| `perm_default` (L79-104) | `--permission-mode default` —— **未触发权限事件**（Bash 直接放行） |
| `perm_disallowed` (L105-121) | `--disallowedTools Bash` —— **启动期黑名单**，模型不知 Bash 存在 |

## 开放问题（M0 未覆盖，影响后续设计）

- [ ] **运行期权限拦截的真正通道**：是 MCP `--permission-prompt-tool` 吗？还是 stream-json
  stdin 上有 `control_request` 之类的消息？需要在 M3 开工前做专门 spike：起一个最小 MCP server
  实现一个权限 tool，看 claude 怎么调它、传什么字段、期望什么返回。
- [ ] **`assistant` 事件相对 `stream_event/message_stop` 的精确顺序**：实测显示 `assistant`
  在 `content_block_stop` 之前就发了（L9 vs L10）——有点反直觉，后端解析时不能假设 message
  完整=stream 结束。
- [ ] **图片 / 附件输入**：stdin user message 里 `content` 是数组时的 image block 形态，留到 M6。
- [ ] **`--include-hook-events` 触发的钩子事件**：M0 场景里没看到一条 hook 事件，可能因为
  当前用户 `~/.claude/settings.json` 没配 hook。需要刻意配一个 PreToolUse hook 复测。
- [ ] **长会话 / 多轮 `--resume`**：恢复后是否会重发 `system.init`？session_id 是否变化？
  M4 开工前抓一遍。
- [ ] **`parent_tool_use_id` 非 None 的情形**：本轮全为 None，应该是 Task agent / subagent 调
  用嵌套时才有。M2 / M5 设计 agent 状态板时再补抓。
- [ ] **rate-limit 触发时的事件形态**：`rate_limit_event.status="allowed"`，没看到限流真实发
  生时的样子。
- [ ] **错误路径**：API 出错 / 模型拒答 / Bash 命令失败时的事件差异。tool_result `is_error=true`
  下的 content 形态没采集到。

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
5. **权限门（M3）暂记**：默认起进程不传 permission-mode（拿到 `bypassPermissions`）；
   M3 spike 后改成 MCP permission tool 模式，由 backend 中转。
