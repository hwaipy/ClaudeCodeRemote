// ClaudeCodeRemote 前端：登录 → 会话列表 → 单会话聊天。
// M2: 工具调用卡片渲染 + tool_result 配对 + 流式参数累积。

const $ = (id) => document.getElementById(id);

const state = {
  token: localStorage.getItem("ccr.token") || "",
  cwd: localStorage.getItem("ccr.cwd") || "",
  ws: null,
  sessionId: null,
  // 流式状态：
  msgById: new Map(),         // msg_id -> {bubble, text}
  toolById: new Map(),         // tool_use_id -> {card, partialInput, finalInput, resultEl}
  activeMsgId: null,           // 当前打开的 stream message id
  blocksByIdx: new Map(),      // stream message index -> {type, msgId|toolUseId}
};

const presets = [
  ["~", "~"],
  ["codes", "~/codes"],
  ["Synology/Claude", "~/SynologyDrive/Claude"],
];

// ---------- 视图切换 ----------
function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  $("view-" + name).classList.add("active");
}

// ---------- HTTP helper ----------
async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  const res = await fetch(path, { ...opts, headers });
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) {
    const err = new Error((body && body.detail) || res.statusText);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

// ---------- 登录 ----------
async function tryLogin(tok) {
  // 用 /api/sessions 当探针
  const saved = state.token;
  state.token = tok;
  try {
    await api("/api/sessions");
    localStorage.setItem("ccr.token", tok);
    return true;
  } catch (e) {
    state.token = saved;
    throw e;
  }
}

$("login-go").addEventListener("click", async () => {
  const tok = $("login-token").value.trim();
  $("login-err").classList.remove("show");
  if (!tok) {
    $("login-err").textContent = "请输入 token";
    $("login-err").classList.add("show");
    return;
  }
  try {
    await tryLogin(tok);
    enterHome();
  } catch (e) {
    $("login-err").textContent = "登录失败：" + (e.message || e);
    $("login-err").classList.add("show");
  }
});
$("login-token").addEventListener("keydown", e => {
  if (e.key === "Enter") $("login-go").click();
});

$("logout").addEventListener("click", (e) => {
  e.preventDefault();
  state.token = "";
  localStorage.removeItem("ccr.token");
  showView("login");
});

// ---------- Home ----------
function renderPresets() {
  const box = $("cwd-presets");
  box.innerHTML = "";
  for (const [label, path] of presets) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = label;
    b.addEventListener("click", () => {
      $("spawn-cwd").value = path;
      syncPresetChips();
    });
    box.appendChild(b);
  }
  syncPresetChips();
}
function syncPresetChips() {
  const v = $("spawn-cwd").value.trim();
  document.querySelectorAll("#cwd-presets .chip").forEach(c => {
    c.classList.toggle("active", c.textContent && presets.find(p => p[0] === c.textContent && p[1] === v));
  });
}
$("spawn-cwd").addEventListener("input", syncPresetChips);

const STATE_BADGES = {
  running:             { label: "运行中", cls: "running" },
  busy:                { label: "工作中", cls: "busy" },
  waiting_permission:  { label: "等批准", cls: "waiting" },
  needs_input:         { label: "等输入", cls: "needs-input" },
  idle:                { label: "空闲",   cls: "idle" },
  hibernated:          { label: "休眠",   cls: "hibernated" },
  finished:            { label: "已结束", cls: "finished" },
};

// 主页状态板：按 sess.id 缓存当前快照
state.sessionsById = new Map();   // id -> session payload
state.globalWS = null;

function relTime(ts) {
  if (!ts) return "";
  const d = Date.now()/1000 - ts;
  if (d < 60)     return Math.max(1, Math.floor(d)) + "s";
  if (d < 3600)   return Math.floor(d/60) + "m";
  if (d < 86400)  return Math.floor(d/3600) + "h";
  return Math.floor(d/86400) + "d";
}

function renderSessionList() {
  const list = $("session-list");
  const arr = Array.from(state.sessionsById.values())
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  if (!arr.length) {
    list.innerHTML = `<div class="session-empty">暂无会话</div>`;
    return;
  }
  list.innerHTML = "";
  for (const s of arr) {
    const badge = STATE_BADGES[s.state] || STATE_BADGES.idle;
    const active = relTime(s.last_activity_at);
    const pp = s.pending_permissions || 0;
    const needs = s.needs_action_detail;
    const el = document.createElement("div");
    el.className = "session-card state-" + (badge.cls || "idle");
    el.innerHTML = `
      <div class="session-row1">
        <div class="name">${escHTML(s.name || "untitled")}</div>
        <span class="state-badge ${badge.cls}">${badge.label}${pp > 1 ? ` ×${pp}` : ""}</span>
        <button class="del-btn" title="删除会话">🗑</button>
      </div>
      <div class="meta">${escHTML(s.cwd)}</div>
      <div class="tiny">${escHTML(s.id)} · 活跃 ${active}前${needs ? " · " + escHTML(needs.slice(0, 40)) : ""}</div>`;
    el.querySelector(".del-btn").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`删除会话 "${s.name}"？不可恢复。`)) return;
      try {
        await api(`/api/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
      } catch (err) {
        alert("删除失败：" + err.message);
      }
    });
    el.addEventListener("click", () => enterChat(s.id, s.name, s.cwd, s.state));
    list.appendChild(el);
  }
}

// in-app toast：会话状态变到 waiting_permission / needs_input 且不在该会话的 chat 视图时提醒
const _lastNotifiedState = new Map();
function maybeNotify(s) {
  // 当前在 home view 时不需要 toast（卡片本身已显眼）
  if ($("view-home").classList.contains("active")) return;
  // 当前正打开这个 session 时不打扰
  if ($("view-chat").classList.contains("active") && state.sessionId === s.id) return;
  const prev = _lastNotifiedState.get(s.id);
  const interesting = s.state === "waiting_permission" || s.state === "needs_input";
  if (interesting && prev !== s.state) {
    showToast(`${s.name} · ${STATE_BADGES[s.state].label}`, s.id);
  }
  _lastNotifiedState.set(s.id, s.state);
}

function showToast(text, sessId) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = text;
  if (sessId) {
    t.style.cursor = "pointer";
    t.addEventListener("click", () => {
      const s = state.sessionsById.get(sessId);
      if (s) enterChat(s.id, s.name, s.cwd, s.state);
    });
  }
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => {
    t.classList.remove("show");
    setTimeout(() => t.remove(), 350);
  }, 4000);
}

function enterHome() {
  showView("home");
  if (!$("spawn-cwd").value) $("spawn-cwd").value = state.cwd || presets[1][1];
  syncPresetChips();
  connectGlobalWS();
}

function connectGlobalWS() {
  if (state.globalWS && state.globalWS.readyState === WebSocket.OPEN) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws-global?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.globalWS = ws;
  ws.addEventListener("message", (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      handleGlobalMsg(msg);
    } catch (e) { console.warn("bad global ws msg", e); }
  });
  ws.addEventListener("close", () => {
    state.globalWS = null;
    // 重连：仅在 home view 时
    if ($("view-home").classList.contains("active")) {
      setTimeout(connectGlobalWS, 2000);
    }
  });
}

function handleGlobalMsg(msg) {
  if (msg.type === "snapshot") {
    state.sessionsById.clear();
    for (const s of msg.sessions || []) state.sessionsById.set(s.id, s);
    renderSessionList();
  } else if (msg.type === "session_state") {
    state.sessionsById.set(msg.id, msg);
    renderSessionList();
    maybeNotify(msg);
  } else if (msg.type === "session_deleted") {
    state.sessionsById.delete(msg.id);
    renderSessionList();
  }
  updateTitleBadge();
}

function updateTitleBadge() {
  let pending = 0;
  for (const s of state.sessionsById.values()) {
    pending += (s.pending_permissions || 0);
  }
  document.title = (pending > 0 ? `[${pending}] ` : "") + "ClaudeCodeRemote";
}

$("spawn-go").addEventListener("click", async () => {
  const name = $("spawn-name").value.trim();
  const cwd = $("spawn-cwd").value.trim();
  $("spawn-err").classList.remove("show");
  if (!cwd) {
    $("spawn-err").textContent = "请填工作目录";
    $("spawn-err").classList.add("show");
    return;
  }
  $("spawn-go").disabled = true;
  $("spawn-go").textContent = "启动中…";
  try {
    const r = await api("/api/spawn", { method: "POST", body: JSON.stringify({ cwd, name }) });
    state.cwd = cwd;
    localStorage.setItem("ccr.cwd", cwd);
    $("spawn-name").value = "";
    enterChat(r.id, r.name, r.cwd);
  } catch (e) {
    $("spawn-err").textContent = "启动失败：" + (e.message || e);
    $("spawn-err").classList.add("show");
  } finally {
    $("spawn-go").disabled = false;
    $("spawn-go").textContent = "启动";
  }
});

// ---------- Chat ----------
async function enterChat(id, name, cwd, sessionState) {
  state.sessionId = id;
  state.msgById.clear();
  state.toolById.clear();
  state.activeMsgId = null;
  state.blocksByIdx.clear();
  $("chat-name").textContent = name || "untitled";
  $("chat-meta").textContent = cwd + " · " + id;
  $("chat-log").innerHTML = "";
  setStatus("connecting", "连接中…");
  showView("chat");
  // 若不是 running，先 resume 拉起子进程
  if (sessionState && sessionState !== "running") {
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}/resume`, { method: "POST" });
    } catch (e) {
      appendBubble("system", `恢复失败：${e.message}`);
    }
  }
  connectWS();
  $("chat-input").focus();
}

$("chat-back").addEventListener("click", () => {
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  enterHome();
});

function setStatus(cls, text) {
  const el = $("chat-status");
  el.classList.remove("busy", "error");
  if (cls === "busy") el.classList.add("busy");
  if (cls === "error") el.classList.add("error");
  el.textContent = text;
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/${encodeURIComponent(state.sessionId)}?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.ws = ws;
  ws.addEventListener("open", () => setStatus("", "已连接"));
  ws.addEventListener("close", (e) => setStatus("error", "断开 " + e.code));
  ws.addEventListener("error", () => setStatus("error", "连接错误"));
  ws.addEventListener("message", (ev) => {
    try {
      const env = JSON.parse(ev.data);
      handleEvent(env.event);
    } catch (e) {
      console.warn("bad ws msg", e, ev.data);
    }
  });
}

function appendBubble(kind, text) {
  const log = $("chat-log");
  const el = document.createElement("div");
  el.className = "bubble " + kind;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function escHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

// ---------- Tool 卡片 ----------
const TOOL_ICONS = {
  Bash: "⌘",
  Read: "📖",
  Write: "✎",
  Edit: "✎",
  Glob: "🔍",
  Grep: "🔎",
  WebFetch: "🌐",
  WebSearch: "🔎",
  Task: "▶",
  TodoWrite: "✓",
};

function ensureToolCard(toolUseId, name) {
  let entry = state.toolById.get(toolUseId);
  if (entry) return entry;
  const log = $("chat-log");
  const card = document.createElement("div");
  card.className = "tool-card";
  card.dataset.toolUseId = toolUseId;
  const icon = TOOL_ICONS[name] || "•";
  card.innerHTML = `
    <div class="tool-head">
      <span class="tool-icon">${escHTML(icon)}</span>
      <span class="tool-name">${escHTML(name || "tool")}</span>
      <span class="tool-status pending">运行中…</span>
    </div>
    <div class="tool-args mono"></div>
    <div class="tool-result" hidden></div>`;
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
  entry = {
    card,
    name: name || "tool",
    partialInput: "",
    finalInput: null,
    argsEl: card.querySelector(".tool-args"),
    resultEl: card.querySelector(".tool-result"),
    statusEl: card.querySelector(".tool-status"),
  };
  state.toolById.set(toolUseId, entry);
  return entry;
}

function renderToolArgs(entry) {
  // 优先用 finalInput（解析过的 dict），否则用 partialInput 原文
  let body;
  if (entry.finalInput && typeof entry.finalInput === "object") {
    body = formatToolInput(entry.name, entry.finalInput);
  } else {
    body = entry.partialInput || "";
  }
  entry.argsEl.textContent = body;
}

function formatToolInput(name, input) {
  if (name === "Bash" && input.command) {
    let s = "$ " + input.command;
    if (input.description) s += "\n# " + input.description;
    return s;
  }
  if ((name === "Read" || name === "Write") && input.file_path) {
    let s = input.file_path;
    if (name === "Write" && input.content != null) {
      s += "\n\n" + truncate(String(input.content), 600);
    }
    return s;
  }
  if (name === "Edit" && input.file_path) {
    return [
      input.file_path,
      "",
      "- " + truncate(String(input.old_string || ""), 300),
      "+ " + truncate(String(input.new_string || ""), 300),
    ].join("\n");
  }
  // 兜底：紧凑 JSON
  try { return JSON.stringify(input, null, 2); }
  catch (e) { return String(input); }
}

function truncate(s, n) {
  if (s.length <= n) return s;
  return s.slice(0, n) + "\n… (" + (s.length - n) + " more chars)";
}

function attachToolResult(toolUseId, content, isError) {
  const entry = state.toolById.get(toolUseId);
  if (!entry) return;
  let body;
  if (typeof content === "string") body = content;
  else if (Array.isArray(content)) {
    body = content.map(c => (c.type === "text" ? c.text : JSON.stringify(c))).join("\n");
  } else body = JSON.stringify(content);
  entry.resultEl.hidden = false;
  entry.resultEl.classList.toggle("error", !!isError);
  entry.resultEl.textContent = body;
  entry.statusEl.textContent = isError ? "失败" : "完成";
  entry.statusEl.className = "tool-status " + (isError ? "error" : "done");
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
}

// ---------- 权限请求卡片 ----------
function showPermissionRequest(evt) {
  const log = $("chat-log");
  // 兜底：如果同一 req_id 卡片已经在 DOM 上了（比如 backlog 已显示，server 又重 push 一份），不重复创建
  if (log.querySelector(`.perm-card[data-req-id="${evt.req_id}"]`)) return;
  const card = document.createElement("div");
  card.className = "perm-card pending";
  card.dataset.reqId = evt.req_id;
  const icon = TOOL_ICONS[evt.tool_name] || "•";
  const argsText = formatToolInput(evt.tool_name, evt.tool_input || {});
  card.innerHTML = `
    <div class="perm-head">
      <span class="perm-warn">⚠</span>
      <span class="tool-icon">${escHTML(icon)}</span>
      <span class="tool-name">${escHTML(evt.tool_name || "tool")}</span>
      <span class="tool-status pending">等待批准</span>
    </div>
    <div class="tool-args mono"></div>
    <div class="perm-actions">
      <button class="perm-btn allow"        data-decision="allow"  data-persist="">允许一次</button>
      <button class="perm-btn allow-tool"   data-decision="allow"  data-persist="tool">始终允许此工具</button>
      <button class="perm-btn allow-cmd"    data-decision="allow"  data-persist="command">始终允许此命令</button>
      <button class="perm-btn deny"         data-decision="deny"   data-persist="">拒绝</button>
    </div>
    <div class="perm-resolved" hidden></div>`;
  card.querySelector(".tool-args").textContent = argsText;
  card.querySelectorAll(".perm-btn").forEach(b => {
    b.addEventListener("click", () => sendDecision(evt.req_id, b.dataset.decision, b.dataset.persist));
  });
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
  setStatus("busy", "等待批准");
}

function sendDecision(req_id, decision, persist) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  // 立即 disable 按钮，避免重复点
  const card = document.querySelector(`.perm-card[data-req-id="${req_id}"]`);
  if (card) {
    card.classList.add("submitting");
    card.querySelectorAll(".perm-btn").forEach(b => b.disabled = true);
  }
  state.ws.send(JSON.stringify({
    type: "permission_decision",
    req_id, decision,
    persist: persist || null,
    reason: decision === "deny" ? "user denied" : "user allowed",
  }));
}

function markPermissionResolved(evt) {
  const card = document.querySelector(`.perm-card[data-req-id="${evt.req_id}"]`);
  if (!card) return;
  card.classList.remove("pending", "submitting");
  const status = card.querySelector(".tool-status");
  const dec = evt.decision;
  let label, sCls, dotCls, prefix;
  if (dec === "allow")      { label = "已允许"; sCls = "done";    dotCls = "allowed"; prefix = "✓ "; }
  else if (dec === "deny")  { label = "已拒绝"; sCls = "error";   dotCls = "denied";  prefix = "✗ "; }
  else                       { label = "已失效"; sCls = "stale";   dotCls = "stale";   prefix = "· "; }
  status.className = "tool-status " + sCls;
  status.textContent = label;
  card.querySelector(".perm-actions").hidden = true;
  const resolved = card.querySelector(".perm-resolved");
  resolved.hidden = false;
  resolved.textContent = prefix + (evt.message || "");
  card.classList.add(dotCls);
  // 如果没有 pending 卡片了，把头部状态从「等待批准」收回
  if (!document.querySelector('.perm-card.pending')) {
    setStatus("", "空闲");
  }
}

// ---------- 事件分发 ----------
function handleEvent(evt) {
  const t = evt && evt.type;
  if (!t) return;

  if (t === "stream_event") return handleStreamEvent(evt.event || {});
  if (t === "assistant")    return handleAssistantMessage(evt.message || {});
  if (t === "user")         return handleUserMessage(evt.message || {});
  if (t === "user_input")   return handleUserInput(evt);
  if (t === "system")       return handleSystem(evt);
  if (t === "result")       return handleResult(evt);
  if (t === "_ccr") {
    if (evt.subtype === "permission_request") return showPermissionRequest(evt);
    if (evt.subtype === "permission_resolved") return markPermissionResolved(evt);
    return;
  }
  if (t === "_internal") {
    if (evt.subtype === "exit") {
      appendBubble("system", `claude 进程退出（rc=${evt.returncode}）`);
      setStatus("error", "已退出");
    }
    return;
  }
}

function handleStreamEvent(ev) {
  const sub = ev.type;
  if (sub === "message_start") {
    const id = ev.message && ev.message.id;
    if (id) {
      state.activeMsgId = id;
      state.blocksByIdx.clear();
      setStatus("busy", "工作中…");
    }
    return;
  }
  if (sub === "content_block_start") {
    const idx = ev.index;
    const cb = ev.content_block || {};
    if (cb.type === "text") {
      if (state.activeMsgId == null) return;
      const bubble = appendBubble("assistant", "");
      state.msgById.set(state.activeMsgId, { bubble, text: "" });
      state.blocksByIdx.set(idx, { type: "text", msgId: state.activeMsgId });
    } else if (cb.type === "tool_use") {
      const entry = ensureToolCard(cb.id, cb.name);
      if (cb.input && Object.keys(cb.input).length) {
        entry.finalInput = cb.input;
        renderToolArgs(entry);
      }
      state.blocksByIdx.set(idx, { type: "tool_use", toolUseId: cb.id });
    }
    return;
  }
  if (sub === "content_block_delta") {
    const idx = ev.index;
    const d = ev.delta || {};
    const block = state.blocksByIdx.get(idx);
    if (!block) return;
    if (d.type === "text_delta" && block.type === "text") {
      const msg = state.msgById.get(block.msgId);
      if (msg) {
        msg.text += d.text || "";
        msg.bubble.textContent = msg.text;
        $("chat-log").scrollTop = $("chat-log").scrollHeight;
      }
    } else if (d.type === "input_json_delta" && block.type === "tool_use") {
      const entry = state.toolById.get(block.toolUseId);
      if (entry) {
        entry.partialInput += d.partial_json || "";
        // 实时尝试 parse；不通则按原文显示
        try {
          entry.finalInput = JSON.parse(entry.partialInput);
        } catch (e) { /* still partial */ }
        renderToolArgs(entry);
      }
    }
    return;
  }
  if (sub === "content_block_stop") {
    // 关闭一个 block；交给 assistant 高层兜底
    return;
  }
  if (sub === "message_stop") {
    state.activeMsgId = null;
    return;
  }
}

function handleAssistantMessage(msg) {
  // 完整 assistant message 兜底：补齐 stream 漏掉的内容；以它为准
  const id = msg.id;
  const blocks = msg.content || [];
  for (const b of blocks) {
    if (b.type === "text") {
      const cur = id && state.msgById.get(id);
      if (cur) {
        cur.text = b.text;
        cur.bubble.textContent = b.text;
      } else if (b.text) {
        const bubble = appendBubble("assistant", b.text);
        if (id) state.msgById.set(id, { bubble, text: b.text });
      }
    } else if (b.type === "tool_use") {
      const entry = ensureToolCard(b.id, b.name);
      entry.finalInput = b.input || {};
      renderToolArgs(entry);
    }
  }
}

function handleUserMessage(msg) {
  const cs = msg.content || [];
  for (const c of cs) {
    if (c.type === "tool_result" && c.tool_use_id) {
      attachToolResult(c.tool_use_id, c.content, c.is_error);
    }
  }
}

function handleUserInput(evt) {
  appendBubble("user", evt.content || "");
}

function handleSystem(evt) {
  if (evt.subtype === "init") {
    setStatus("busy", "已就绪");
    // claude 自身 session_id 只显示，不替换我们 state.sessionId（ccr- 永久 id）
    if (evt.session_id) {
      const base = $("chat-meta").textContent.split(" · ")[0];
      $("chat-meta").textContent = `${base} · ${state.sessionId} · claude=${evt.session_id}`;
    }
    appendBubble("system", `init · model=${evt.model} · cwd=${evt.cwd}`);
  } else if (evt.subtype === "post_turn_summary") {
    setStatus("", "空闲");
  } else if (evt.subtype === "hook_started") {
    // hook 已开始执行：通常我们的桥接器在跑，可以忽略
  } else if (evt.subtype === "hook_response") {
    // hook 已返回：决定已经走完了；不额外渲染
  }
}

function handleResult(evt) {
  const cost = (evt.total_cost_usd || 0).toFixed(4);
  appendBubble("system",
    `result · stop=${evt.stop_reason} · turns=${evt.num_turns} · $${cost}`);
  setStatus("", "完成");
}

function sendUserMessage() {
  const ta = $("chat-input");
  const text = ta.value.trim();
  if (!text || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.ws.send(JSON.stringify({ type: "user_message", content: text }));
  // user bubble 等 server 注入 user_input echo 时再渲染（保证刷新/resume 也能看到）
  ta.value = "";
  ta.style.height = "auto";
  setStatus("busy", "等待回复…");
}

$("chat-send").addEventListener("click", sendUserMessage);
$("chat-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendUserMessage();
  }
});
$("chat-input").addEventListener("input", e => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(160, e.target.scrollHeight) + "px";
});

// ---------- 启动 ----------
renderPresets();
if (state.token) {
  tryLogin(state.token).then(enterHome).catch(() => {
    state.token = "";
    localStorage.removeItem("ccr.token");
    showView("login");
  });
} else {
  showView("login");
}
