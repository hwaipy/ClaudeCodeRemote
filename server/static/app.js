// ClaudeCodeRemote M1 前端：登录 → 会话列表 → 单会话裸聊。
// 渲染范围：assistant text、用户输入、system init/post_turn_summary 简略提示。
// 工具调用先不渲染（M2 再做）。

const $ = (id) => document.getElementById(id);

const state = {
  token: localStorage.getItem("ccr.token") || "",
  cwd: localStorage.getItem("ccr.cwd") || "",
  ws: null,
  sessionId: null,
  // 流式：每条 assistant message 一个 bubble；按 message id 索引
  currentMessages: new Map(),  // msg_id -> {el, text}
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

async function refreshSessions() {
  const list = $("session-list");
  try {
    const j = await api("/api/sessions");
    const arr = j.sessions || [];
    if (!arr.length) {
      list.innerHTML = `<div class="session-empty">暂无会话</div>`;
      return;
    }
    list.innerHTML = "";
    for (const s of arr) {
      const el = document.createElement("div");
      el.className = "session-card";
      const finishedTag = s.finished ? '<span class="tiny"> · 已结束</span>' : '';
      el.innerHTML = `
        <div class="name">${escHTML(s.name || "untitled")}${finishedTag}</div>
        <div class="meta">${escHTML(s.cwd)}</div>
        <div class="tiny">${escHTML(s.id)}</div>`;
      el.addEventListener("click", () => enterChat(s.id, s.name, s.cwd));
      list.appendChild(el);
    }
  } catch (e) {
    list.innerHTML = `<div class="err show">加载失败：${escHTML(e.message)}</div>`;
  }
}

function enterHome() {
  showView("home");
  if (!$("spawn-cwd").value) $("spawn-cwd").value = state.cwd || presets[1][1];
  syncPresetChips();
  refreshSessions();
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
function enterChat(id, name, cwd) {
  state.sessionId = id;
  state.currentMessages.clear();
  $("chat-name").textContent = name || "untitled";
  $("chat-meta").textContent = cwd + " · " + id;
  $("chat-log").innerHTML = "";
  setStatus("connecting", "连接中…");
  showView("chat");
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
      handleEvent(env.event, env);
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

function handleEvent(evt, env) {
  const t = evt && evt.type;
  if (!t) return;

  if (t === "stream_event") {
    const sub = evt.event && evt.event.type;
    if (sub === "message_start") {
      const id = evt.event.message && evt.event.message.id;
      if (id) {
        const el = appendBubble("assistant", "");
        state.currentMessages.set(id, { el, text: "" });
        setStatus("busy", "工作中…");
      }
    } else if (sub === "content_block_delta") {
      const d = evt.event.delta;
      if (d && d.type === "text_delta") {
        // 流式 append。问题：哪个 message id？stream_event 不带 message id
        // 在 content_block_delta 上。我们假设是「最近一个 message_start 的 message」。
        const last = lastAssistantBubble();
        if (last) {
          last.text += d.text || "";
          last.el.textContent = last.text;
          $("chat-log").scrollTop = $("chat-log").scrollHeight;
        }
      }
    } else if (sub === "message_stop") {
      // 不做事；assistant 高层事件会给我们完整 message
    }
    return;
  }

  if (t === "assistant") {
    // 完整 assistant message：兜底用它替换 bubble 文本（覆盖 stream 拼的，避免漂移）
    const msg = evt.message || {};
    const id = msg.id;
    const cur = id && state.currentMessages.get(id);
    const blocks = msg.content || [];
    const text = blocks.filter(b => b.type === "text").map(b => b.text).join("");
    if (cur) {
      cur.text = text;
      cur.el.textContent = text;
    } else if (text) {
      const el = appendBubble("assistant", text);
      if (id) state.currentMessages.set(id, { el, text });
    }
    // 工具调用先不渲染，M2 再加
    const tools = blocks.filter(b => b.type === "tool_use");
    if (tools.length) {
      for (const tu of tools) {
        appendBubble("system", `[tool_use ${tu.name}]  (M2 才会渲染参数/结果)`);
      }
    }
    return;
  }

  if (t === "user") {
    // tool_result 回喂。M1 不展开，简略提示
    const cs = (evt.message && evt.message.content) || [];
    for (const c of cs) {
      if (c.type === "tool_result") {
        const head = c.is_error ? "[tool_result · error]" : "[tool_result]";
        // M1 只显示前 80 字符
        const content = typeof c.content === "string" ? c.content : JSON.stringify(c.content);
        appendBubble("system", `${head} ${content.slice(0, 80)}${content.length > 80 ? "…" : ""}`);
      }
    }
    return;
  }

  if (t === "system") {
    if (evt.subtype === "init") {
      setStatus("busy", "已就绪");
      // 若 session id 从 pending 切换成真实 UUID，同步 url
      if (evt.session_id && evt.session_id !== state.sessionId) {
        state.sessionId = evt.session_id;
        $("chat-meta").textContent = ($("chat-meta").textContent.split(" · ")[0]) + " · " + evt.session_id;
      }
      appendBubble("system", `init · model=${evt.model} · cwd=${evt.cwd}`);
    } else if (evt.subtype === "post_turn_summary") {
      setStatus("", "空闲");
    }
    return;
  }

  if (t === "result") {
    const cost = (evt.total_cost_usd || 0).toFixed(4);
    appendBubble("system", `result · stop=${evt.stop_reason} · turns=${evt.num_turns} · $${cost}`);
    setStatus("", "完成");
    return;
  }

  if (t === "_internal" && evt.subtype === "exit") {
    appendBubble("system", `claude 进程退出（rc=${evt.returncode}）`);
    setStatus("error", "已退出");
    return;
  }
}

function lastAssistantBubble() {
  // 拿 Map 里最后一个；Map 按插入顺序
  let last = null;
  for (const v of state.currentMessages.values()) last = v;
  return last;
}

function sendUserMessage() {
  const ta = $("chat-input");
  const text = ta.value.trim();
  if (!text || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  appendBubble("user", text);
  state.ws.send(JSON.stringify({ type: "user_message", content: text }));
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
