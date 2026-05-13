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

// ---------- 主题 ----------
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t === "dark" ? "dark" : "light");
  try { localStorage.setItem("ccr.theme", t); } catch (e) {}
}
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "light";
}
function toggleTheme() {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
}
// 初始化主题（早于 DOM 其它操作）
(function initTheme() {
  let t = "light";
  try { t = localStorage.getItem("ccr.theme") || "light"; } catch (e) {}
  applyTheme(t);
})();

// ---------- 视图切换 ----------
function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  $("view-" + name).classList.add("active");
  // body 阶段：登录前 / 登录后双栏（仅 CSS @media 大屏生效）
  document.body.classList.remove("stage-login", "stage-app");
  document.body.classList.add(name === "login" ? "stage-login" : "stage-app");
}

// ---------- HTTP helper ----------
// 所有路径都用相对（不带前导 /），让 <base href> 拼前缀；反代到 /remote/
// 下时自动变 /remote/api/xxx。
function apiPath(p) {
  return p.replace(/^\/+/, "");  // 兼容老调用方传 "/api/..."
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  const res = await fetch(apiPath(path), { ...opts, headers });
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

// WS 必须用绝对 URL；基于 <base href> 解析相对路径，再切 ws/wss 协议
function wsURL(relPath) {
  const u = new URL(relPath, document.baseURI);
  u.protocol = (u.protocol === "https:") ? "wss:" : "ws:";
  return u.toString();
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
  state.sessionId = null;
  localStorage.removeItem("ccr.token");
  document.body.classList.remove("has-session");
  showView("login");
});

const _hardReloadEl = $("hard-reload");
if (_hardReloadEl) _hardReloadEl.addEventListener("click", async (e) => {
  e.preventDefault();
  try {
    if ("serviceWorker" in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.unregister()));
    }
    if (window.caches) {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    }
  } finally {
    // 带 cache-busting query：iOS standalone 的 WebKit HTTP 缓存不归 SW 管，
    // 改 URL 才能保证拿新 HTML
    const u = new URL(location.href);
    u.searchParams.set("_r", Date.now().toString());
    location.replace(u.toString());
  }
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

// ---------- 目录浏览 modal ----------
const _browse = { curPath: "" };
async function browseLoad(path) {
  const list = $("modal-list");
  list.innerHTML = '<div class="modal-empty">加载中…</div>';
  try {
    const j = await api(`api/ls?path=${encodeURIComponent(path || "")}`);
    _browse.curPath = j.path;
    $("modal-crumb").textContent = j.path;
    const rows = [];
    if (j.parent !== null) {
      rows.push(`<div class="modal-row parent" data-path="${escHTML(j.parent)}"><span class="icon">↰</span><span class="name">.. (上一级)</span></div>`);
    }
    for (const d of j.dirs) {
      const child = j.path === "/" ? "/" + d : j.path + "/" + d;
      rows.push(`<div class="modal-row" data-path="${escHTML(child)}"><span class="icon">📁</span><span class="name">${escHTML(d)}</span></div>`);
    }
    if (!j.dirs.length && j.parent === null) {
      rows.push('<div class="modal-empty">（无子目录）</div>');
    } else if (!j.dirs.length) {
      rows.push('<div class="modal-empty">（无子目录）</div>');
    }
    list.innerHTML = rows.join("");
    list.querySelectorAll(".modal-row").forEach(el => {
      el.addEventListener("click", () => browseLoad(el.dataset.path));
    });
  } catch (e) {
    list.innerHTML = `<div class="modal-empty err show">加载失败：${escHTML(e.message)}</div>`;
  }
}
function openBrowse() {
  $("modal-browse").hidden = false;
  browseLoad($("spawn-cwd").value.trim() || "~");
}
function closeBrowse() {
  $("modal-browse").hidden = true;
}
$("browse-btn").addEventListener("click", openBrowse);
$("modal-close").addEventListener("click", closeBrowse);
$("modal-cancel").addEventListener("click", closeBrowse);
$("modal-confirm").addEventListener("click", () => {
  if (_browse.curPath) {
    $("spawn-cwd").value = _browse.curPath;
    syncPresetChips();
  }
  closeBrowse();
});
$("modal-browse").addEventListener("click", (e) => {
  if (e.target.id === "modal-browse") closeBrowse();
});

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
state.attachments = [];           // 待发送附件列表：[{kind, media_type, base64, dataUrl}]

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

let _globalBackoff = 1000;
let _globalTimer = null;
function connectGlobalWS() {
  if (state.globalWS
      && (state.globalWS.readyState === WebSocket.OPEN
          || state.globalWS.readyState === WebSocket.CONNECTING)) return;
  if (!state.token) return;   // 未登录
  if (_globalTimer) { clearTimeout(_globalTimer); _globalTimer = null; }
  const url = wsURL("ws-global?token=" + encodeURIComponent(state.token));
  const ws = new WebSocket(url);
  state.globalWS = ws;
  ws.addEventListener("open", () => { _globalBackoff = 1000; });
  ws.addEventListener("message", (ev) => {
    try { handleGlobalMsg(JSON.parse(ev.data)); }
    catch (e) { console.warn("bad global ws msg", e); }
  });
  ws.addEventListener("close", () => {
    if (state.globalWS === ws) state.globalWS = null;
    if (!state.token) return;
    _globalTimer = setTimeout(connectGlobalWS, _globalBackoff);
    _globalBackoff = Math.min(30000, _globalBackoff * 2);
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
  // 切 session 前必须先关旧 ws：否则旧 session 的事件会写到新 session 的 chat-log 上（串 session）
  if (state.ws) {
    try { state.ws.close(); } catch (_) {}
    state.ws = null;
  }
  state.sessionId = id;
  document.body.classList.add("has-session");
  state.msgById.clear();
  state.toolById.clear();
  state.activeMsgId = null;
  state.blocksByIdx.clear();
  // 切 session 必须把翻页状态一并重置：不然 chat-log.innerHTML="" 触发的 scroll 事件会
  // 用旧 session 的 firstSeq + 新 session 的 sessionId 调一次野的 loadEarlierHistory
  state.firstSeq = null;
  state.hasMoreHistory = false;
  state.loadingHistory = false;
  state.suppressScrollLoad = true;
  setTimeout(() => { state.suppressScrollLoad = false; }, 500);
  // 清掉上一次可能残留的 inline transform/transition，避免影响这次滑入动画
  const _chatView = $("view-chat");
  _chatView.style.transform = "";
  _chatView.style.transition = "";
  $("chat-name").textContent = name || "untitled";
  // 只显示 cwd 末尾两段，足够辨识又不啰嗦
  $("chat-meta").textContent = (cwd || "").split("/").slice(-2).join("/") || cwd || "";
  $("chat-log").innerHTML = "";
  setStatus("connecting", "连接中…");
  // 注意：不立即 showView("chat")，先让 backlog 在屏外渲染完再揭幕
  if (sessionState && sessionState !== "running") {
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}/resume`, { method: "POST" });
    } catch (e) {
      appendBubble("system", `恢复失败：${e.message}`);
    }
  }
  // 收到 backlog_done 后才滑入；800ms 兜底防 server 异常不发标记
  let revealed = false;
  const ownId = id;
  const reveal = () => {
    if (revealed) return;
    revealed = true;
    if (state.sessionId !== ownId) return;   // 用户已切到别的 session，不要错误揭幕
    const log = $("chat-log");
    setScrollTopInstant(log, log.scrollHeight);   // 进场前瞬时贴底，避免被 smooth 化看到滚动过程
    showView("chat");
    // 移动端不 auto-focus：软键盘弹出与滑入动画同时进行，WebKit 会渲染异常导致卡在半屏
    if (window.innerWidth >= 900) $("chat-input").focus();
  };
  state.revealChat = reveal;
  setTimeout(reveal, 800);
  connectWS();
}

$("chat-back").addEventListener("click", () => {
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  state.sessionId = null;
  document.body.classList.remove("has-session");
  enterHome();
});

// 左边缘右滑返回：跟手实时拖 chat 视图，松手按位移判定。仿 iOS 原生手势，窄屏单栏才启用
(function setupSwipeBack() {
  const view = $("view-chat");
  if (!view) return;
  const EDGE = 24;            // 起手必须在距左边 24px 以内
  const SLOP = 8;              // 决定方向前的容差
  const COMMIT_FRAC = 0.35;    // 松手时位移超过这个比例 → 继续滑出返回
  const COMMIT_VELOCITY = 0.5; // px/ms，速度足够也直接返回
  let armed = false;           // 起手在边缘内，但还没确定是横向手势
  let dragging = false;        // 已确认是横向手势，跟手中
  let startX = 0, startY = 0, startT = 0, lastX = 0, lastT = 0, width = 0;

  function endTransition(target, onEnd) {
    // 跟手松手后的回弹/滑完用一个偏短的 transition，保持利落
    view.style.transition = "transform 260ms cubic-bezier(0.25, 1, 0.5, 1)";
    let done = false;
    const fire = () => {
      if (done) return;
      done = true;
      view.removeEventListener("transitionend", fire);
      view.style.transition = "";   // 恢复 CSS 默认（用于下次自动滑入/出）
      onEnd && onEnd();
    };
    view.addEventListener("transitionend", fire);
    setTimeout(fire, 320);          // 兜底：transitionend 偶尔不触发
    view.style.transform = target;
  }

  view.addEventListener("touchstart", e => {
    if (window.innerWidth >= 900) return;
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    if (t.clientX > EDGE) return;
    armed = true; dragging = false;
    startX = lastX = t.clientX;
    startY = t.clientY;
    startT = lastT = e.timeStamp;
    width = view.offsetWidth || window.innerWidth;
  }, { passive: true });

  view.addEventListener("touchmove", e => {
    if (!armed) return;
    const t = e.touches[0];
    const dx = t.clientX - startX;
    const dy = t.clientY - startY;
    if (!dragging) {
      if (Math.abs(dy) > SLOP && Math.abs(dy) > Math.abs(dx)) { armed = false; return; }
      if (Math.abs(dx) <= SLOP) return;
      dragging = true;
      view.style.transition = "none";   // 跟手阶段禁用过渡
    }
    // 一旦确认是右滑返回手势，吃掉所有 touchmove，避免手指上下动时 chat-log 同时滚动
    if (e.cancelable) e.preventDefault();
    const tx = Math.max(0, dx);
    view.style.transform = `translateX(${tx}px)`;
    lastX = t.clientX;
    lastT = e.timeStamp;
  }, { passive: false });

  function release(e) {
    if (!armed) return;
    armed = false;
    if (!dragging) return;
    dragging = false;
    const t = (e.changedTouches && e.changedTouches[0]) || { clientX: lastX, timeStamp: lastT };
    const dx = t.clientX - startX;
    const dt = Math.max(1, (e.timeStamp || lastT) - lastT);
    const v = (t.clientX - lastX) / dt;  // px/ms，最后一段速度
    const commit = dx > width * COMMIT_FRAC || v > COMMIT_VELOCITY;
    if (commit) {
      // 继续滑到 100%，结束后 leaveChat 切回 home（chat 已经在外，无视觉跳跃）
      endTransition(`translateX(${width}px)`, () => {
        // 顺序：先移除 .active（CSS 默认 transform:100%），再清 inline；
        // 反过来会让 chat 瞬间跳回 0（有 .active）再滑出去，看起来像"卡在中间"
        $("chat-back").click();
        view.style.transform = "";
      });
    } else {
      endTransition("translateX(0)", () => { view.style.transform = ""; });
    }
  }
  view.addEventListener("touchend", release, { passive: true });
  view.addEventListener("touchcancel", () => {
    // 取消视为回原位
    if (dragging) endTransition("translateX(0)", () => { view.style.transform = ""; });
    armed = false; dragging = false;
  }, { passive: true });
})();

function setStatus(cls, text) {
  const el = $("chat-status");
  el.classList.remove("busy", "error");
  if (cls === "busy") el.classList.add("busy");
  if (cls === "error") el.classList.add("error");
  el.textContent = text;
}

// 不在底端时显示的"回到最新"按钮：位置跟随 chat-log 右下角（避开 chat-foot）
function syncScrollToBottomBtn() {
  const log = $("chat-log");
  const btn = $("scroll-to-bottom");
  if (!btn) return;
  const distFromBottom = log.scrollHeight - log.scrollTop - log.clientHeight;
  const atBottom = distFromBottom < 40;
  btn.hidden = atBottom;
  if (atBottom) return;
  const r = log.getBoundingClientRect();
  btn.style.left = (r.right - 16 - 36) + "px";
  btn.style.top = (r.bottom - 16 - 36) + "px";
}
$("scroll-to-bottom").addEventListener("click", () => {
  const log = $("chat-log");
  log.scrollTo({ top: log.scrollHeight, behavior: "smooth" });
});

function setHistoryLoader(text) {
  // loader 是 view-chat 内的 fixed 元素，跟 chat-log 完全无关，不影响其 layout/scroll
  const el = $("history-loader");
  if (!el) return;
  if (text == null) { el.hidden = true; return; }
  el.hidden = false;
  el.querySelector(".history-text").textContent = text;
  const r = $("chat-log").getBoundingClientRect();
  el.style.top = (r.top + 8) + "px";
  el.style.left = (r.left + r.width / 2) + "px";
}

// chat-log 有 scroll-behavior: smooth；某些场景（保持位置、进场贴底）必须瞬时
function setScrollTopInstant(el, value) {
  const prev = el.style.scrollBehavior;
  el.style.scrollBehavior = "auto";
  el.scrollTop = value;
  el.style.scrollBehavior = prev;
}

function waitForScrollIdle(el, idleMs = 200, maxWait = 2000) {
  return new Promise(resolve => {
    let idleTimer, maxTimer;
    const done = () => {
      el.removeEventListener("scroll", onScroll);
      clearTimeout(idleTimer);
      clearTimeout(maxTimer);
      resolve();
    };
    const onScroll = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(done, idleMs);
    };
    el.addEventListener("scroll", onScroll);
    idleTimer = setTimeout(done, idleMs);   // 已经静止：直接完成
    maxTimer = setTimeout(done, maxWait);   // 兜底：极端情况下也不会卡死
  });
}

async function loadEarlierHistory() {
  if (!state.sessionId || !state.hasMoreHistory || state.loadingHistory) return;
  if (state.firstSeq == null) return;
  const log = $("chat-log");
  const beforeSeq = state.firstSeq;
  state.loadingHistory = true;
  state.suppressScrollLoad = true;   // 整个加载期间屏蔽 scroll listener 触发；纠偏 set scrollTop 会触发 scroll
  setHistoryLoader("加载更早的消息…");
  const savedBehavior = log.style.scrollBehavior;
  log.style.scrollBehavior = "auto";
  void log.offsetHeight;
  const firstExisting = log.firstChild;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/messages?before_seq=${beforeSeq}&limit=20`);
    const earlier = data.messages || [];
    if (earlier.length === 0) {
      state.hasMoreHistory = false;
      setHistoryLoader(null);
      return;
    }
    // 等滑动惯性彻底停下再渲染：iOS momentum 期间 set scrollTop 会被覆盖
    await waitForScrollIdle(log);
    // idle 之后 anchor 用最新的视口位置（用户可能在等待期间滚到了别处）
    const idleRect = firstExisting ? firstExisting.getBoundingClientRect() : null;
    const existing = [];
    while (log.firstChild) existing.push(log.removeChild(log.firstChild));
    for (const env of earlier) {
      try { handleEvent(env.event); } catch (e) { console.warn("history render error", e); }
    }
    for (const node of existing) log.appendChild(node);
    state.firstSeq = data.first_seq != null ? data.first_seq : earlier[0].seq;
    state.hasMoreHistory = !!data.has_more;
    void log.offsetHeight;
    // 同步纠偏到 sub-pixel
    if (firstExisting && idleRect) {
      for (let i = 0; i < 6; i++) {
        void log.offsetHeight;
        const drift = firstExisting.getBoundingClientRect().top - idleRect.top;
        if (Math.abs(drift) < 0.05) break;
        log.scrollTop += drift;
      }
    }
    setHistoryLoader(null);
  } catch (e) {
    console.warn("loadEarlierHistory failed", e);
    setHistoryLoader("加载失败，下拉重试");
  } finally {
    state.loadingHistory = false;
    log.style.scrollBehavior = savedBehavior;
    // 延后再开放 scroll 触发：让 set scrollTop 排队的 scroll event 处理完不会被当成新的滑到顶
    setTimeout(() => { state.suppressScrollLoad = false; }, 300);
  }
}

// chat-log 滚到顶部附近时拉更早历史；wheel/touch 顶部继续上拉也触发（chat-log 不可滚时兜底）
$("chat-log").addEventListener("scroll", () => {
  syncScrollToBottomBtn();
  if (state.suppressScrollLoad) return;
  if ($("chat-log").scrollTop < 100) loadEarlierHistory();
});
window.addEventListener("resize", syncScrollToBottomBtn);
window.addEventListener("orientationchange", syncScrollToBottomBtn);
$("chat-log").addEventListener("wheel", (e) => {
  if (state.suppressScrollLoad) return;
  if (e.deltaY < 0 && $("chat-log").scrollTop < 100) loadEarlierHistory();
});
let _touchStartY = 0;
$("chat-log").addEventListener("touchstart", e => { _touchStartY = e.touches[0].clientY; }, { passive: true });
$("chat-log").addEventListener("touchmove", e => {
  if (state.suppressScrollLoad) return;
  if ($("chat-log").scrollTop < 5 && e.touches[0].clientY > _touchStartY + 40) loadEarlierHistory();
}, { passive: true });

function connectWS() {
  const ownSessionId = state.sessionId;     // 闭包捕获：用户切到别的 session 时旧实例自动失效
  const url = wsURL("ws/" + encodeURIComponent(ownSessionId)
                    + "?token=" + encodeURIComponent(state.token));
  let backoff = 1000;
  let timer = null;
  const isOwn = () => state.sessionId === ownSessionId;

  function start() {
    if (!isOwn()) return;
    if (state.ws
        && (state.ws.readyState === WebSocket.OPEN
            || state.ws.readyState === WebSocket.CONNECTING)) return;
    const ws = new WebSocket(url);
    state.ws = ws;
    const isCurrent = () => state.ws === ws && isOwn();
    ws.addEventListener("open", () => {
      if (!isCurrent()) return;
      setStatus("", "已连接");
      backoff = 1000;
      // 重连后服务端会重发初始 history + backlog_done，先清显示避免重复
      $("chat-log").innerHTML = "";
      state.msgById.clear();
      state.toolById.clear();
      state.activeMsgId = null;
      state.blocksByIdx.clear();
      state.firstSeq = null;
      state.hasMoreHistory = false;
      state.loadingHistory = false;
      // ws 重连场景：等接下来的 backlog_done 重新贴底（revealChat 是 enterChat 一次性的，不够用）
      state.pendingScrollToBottomOnBacklog = true;
    });
    ws.addEventListener("close", () => {
      if (!isCurrent()) return;
      const secs = Math.round(backoff / 1000);
      setStatus("error", `断开，${secs}s 后重连`);
      if (timer) clearTimeout(timer);
      timer = setTimeout(start, backoff);
      backoff = Math.min(30000, backoff * 2);
    });
    ws.addEventListener("error", () => { if (isCurrent()) setStatus("error", "连接错误"); });
    ws.addEventListener("message", (ev) => {
      if (!isCurrent()) return;
      try { handleEvent(JSON.parse(ev.data).event); }
      catch (e) { console.warn("bad ws msg", e, ev.data); }
    });
  }

  // 暴露立即重连入口给 visibilitychange / online 等主动唤醒源使用
  state.reconnectChatNow = () => {
    if (!isOwn()) return;
    backoff = 1000;
    if (timer) { clearTimeout(timer); timer = null; }
    const ws = state.ws;
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
      start();
    }
  };
  start();
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

// ---------- markdown 渲染 ----------
// marked.js 解析 + DOM sanitizer 去掉 script/iframe/javascript: URL，
// 链接强制 target=_blank rel=noopener。
if (typeof marked !== "undefined") {
  marked.setOptions({ gfm: true, breaks: true, headerIds: false, mangle: false });
}
const _UNSAFE_TAGS = new Set([
  "SCRIPT","STYLE","IFRAME","OBJECT","EMBED","FORM","INPUT","TEXTAREA",
  "BUTTON","LINK","META","BASE"
]);
function sanitizeMD(html) {
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  const walk = (el) => {
    [...el.children].forEach(child => {
      if (_UNSAFE_TAGS.has(child.tagName)) { child.remove(); return; }
      // 去 on* 事件、javascript: URL
      [...child.attributes].forEach(a => {
        const n = a.name.toLowerCase();
        if (n.startsWith("on")) child.removeAttribute(a.name);
        if ((n === "href" || n === "src") && /^\s*javascript:/i.test(a.value)) {
          child.removeAttribute(a.name);
        }
      });
      if (child.tagName === "A") {
        child.setAttribute("target", "_blank");
        child.setAttribute("rel", "noopener noreferrer");
      }
      walk(child);
    });
  };
  walk(tmp);
  return tmp.innerHTML;
}
function renderMarkdown(text) {
  if (!text) return "";
  if (typeof marked === "undefined") return escHTML(text).replace(/\n/g, "<br>");
  try {
    // 公式占位：避免 marked 把 $a*b$ 里的 * 解析成 emphasis
    const stash = [];
    const stashIt = (s) => { stash.push(s); return `@@CCRMATH${stash.length - 1}@@`; };
    let s = String(text);
    s = s.replace(/\$\$([\s\S]+?)\$\$/g, m => stashIt(m));
    s = s.replace(/\\\[([\s\S]+?)\\\]/g, m => stashIt(m));
    s = s.replace(/\\\(([\s\S]+?)\\\)/g, m => stashIt(m));
    s = s.replace(/\$([^$\n]+?)\$/g, m => stashIt(m));
    let html = sanitizeMD(marked.parse(s));
    // 还原（escHTML 是 KaTeX-safe 的：< / > 会被还原为字符）
    html = html.replace(/@@CCRMATH(\d+)@@/g, (_, i) => escHTML(stash[Number(i)]));
    return html;
  } catch (e) {
    console.warn("markdown parse failed:", e);
    return escHTML(text).replace(/\n/g, "<br>");
  }
}

// KaTeX 数学公式：marked 渲染后再扫描元素里的 $...$ / $$...$$ / \(...\) / \[...\]
function renderMathIn(el) {
  if (typeof renderMathInElement === "undefined" || !el) return;
  try {
    renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "\\(", right: "\\)", display: false },
        { left: "$",  right: "$",  display: false },
      ],
      throwOnError: false,
      errorColor: "var(--danger-fg, #d70015)",
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    });
  } catch (e) {
    console.warn("katex render failed:", e);
  }
}

// markdown + 数学的复合渲染（DOM 写入 + 公式重排）
function renderMDIntoBubble(bubble, text) {
  bubble.innerHTML = renderMarkdown(text);
  renderMathIn(bubble);
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
  card.className = "tool-card collapsed";   // 全部默认收起，包括运行中的
  card.dataset.toolUseId = toolUseId;
  const icon = TOOL_ICONS[name] || "•";
  card.innerHTML = `
    <div class="tool-head" role="button" tabindex="0">
      <span class="tool-icon">${escHTML(icon)}</span>
      <span class="tool-name">${escHTML(name || "tool")}</span>
      <span class="tool-summary"></span>
      <span class="tool-status pending"></span>
    </div>
    <div class="tool-body">
      <div class="tool-args mono"></div>
      <div class="tool-result" hidden></div>
    </div>`;
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
  const headEl = card.querySelector(".tool-head");
  const toggle = () => card.classList.toggle("collapsed");
  headEl.addEventListener("click", toggle);
  headEl.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  entry = {
    card,
    name: name || "tool",
    partialInput: "",
    finalInput: null,
    argsEl: card.querySelector(".tool-args"),
    resultEl: card.querySelector(".tool-result"),
    statusEl: card.querySelector(".tool-status"),
    summaryEl: card.querySelector(".tool-summary"),
  };
  state.toolById.set(toolUseId, entry);
  return entry;
}

function toolSummary(name, input) {
  if (!input || typeof input !== "object") return "";
  if (name === "Bash") return (input.command || "").split("\n")[0];
  if (name === "Read" || name === "Write" || name === "Edit") {
    const p = input.file_path || "";
    return p.split("/").slice(-2).join("/");
  }
  if (name === "Glob" || name === "Grep") return input.pattern || "";
  if (name === "WebFetch") return input.url || "";
  if (name === "WebSearch") return input.query || "";
  if (name === "TodoWrite") {
    const n = Array.isArray(input.todos) ? input.todos.length : 0;
    return n ? `${n} 项` : "";
  }
  return "";
}

// 从尚未完整的 partial_json 里用正则提取关键字段，让 tool 卡头在流式参数收集阶段也能显示路径/命令
function partialSummary(name, partial) {
  if (!partial) return "";
  const grab = (key) => {
    // 简化匹配：value 是一段不含未转义引号的字符；JSON 转义还原最常见的 \" \\ \n
    const m = new RegExp('"' + key + '"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"').exec(partial);
    if (!m) return null;
    return m[1].replace(/\\"/g, '"').replace(/\\\\/g, "\\").replace(/\\n/g, "\n");
  };
  if (name === "Bash") {
    const v = grab("command"); return v ? v.split("\n")[0] : "";
  }
  if (name === "Read" || name === "Write" || name === "Edit") {
    const v = grab("file_path"); return v ? v.split("/").slice(-2).join("/") : "";
  }
  if (name === "Glob" || name === "Grep") return grab("pattern") || "";
  if (name === "WebFetch") return grab("url") || "";
  if (name === "WebSearch") return grab("query") || "";
  return "";
}

function renderToolArgs(entry) {
  // 一行摘要：优先用 finalInput；finalInput 还没好就从 partialInput 用正则提
  if (entry.summaryEl) {
    let s = "";
    if (entry.finalInput && typeof entry.finalInput === "object") {
      s = toolSummary(entry.name, entry.finalInput);
    } else {
      s = partialSummary(entry.name, entry.partialInput);
    }
    entry.summaryEl.textContent = s;
  }
  // Edit 走 DOM diff 视图；其它走纯文本 formatToolInput
  if (entry.name === "Edit"
      && entry.finalInput && typeof entry.finalInput === "object"
      && entry.finalInput.file_path) {
    renderEditDiff(entry);
    return;
  }
  let body;
  if (entry.finalInput && typeof entry.finalInput === "object") {
    body = formatToolInput(entry.name, entry.finalInput);
  } else {
    body = entry.partialInput || "";
  }
  entry.argsEl.textContent = body;
}

// 行级 unified diff（简单 LCS DP）
function unifiedDiff(oldText, newText) {
  const a = String(oldText).split("\n");
  const b = String(newText).split("\n");
  const m = a.length, n = b.length;
  // dp[i][j] = LCS length of a[i..m-1] and b[j..n-1]
  const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) { ops.push({ op: " ", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ op: "-", text: a[i] }); i++; }
    else                                     { ops.push({ op: "+", text: b[j] }); j++; }
  }
  while (i < m) { ops.push({ op: "-", text: a[i] }); i++; }
  while (j < n) { ops.push({ op: "+", text: b[j] }); j++; }
  return ops;
}

function renderEditDiff(entry) {
  const inp = entry.finalInput;
  entry.argsEl.innerHTML = "";
  const header = document.createElement("div");
  header.className = "diff-header";
  const path = String(inp.file_path);
  header.innerHTML = `<span class="diff-path">${escHTML(path)}</span>`;
  // VS Code 跳转链接：vscode://file/<abs>
  if (path.startsWith("/")) {
    const a = document.createElement("a");
    a.href = "vscode://file" + path;
    a.target = "_blank";
    a.rel = "noopener";
    a.className = "diff-link";
    a.textContent = "↗ VS Code";
    header.appendChild(a);
  }
  entry.argsEl.appendChild(header);

  const body = document.createElement("div");
  body.className = "diff-body";
  const ops = unifiedDiff(inp.old_string || "", inp.new_string || "");
  for (const o of ops) {
    const row = document.createElement("div");
    row.className = "diff-row " + (o.op === "-" ? "del" : o.op === "+" ? "ins" : "ctx");
    row.textContent = (o.op === " " ? "  " : o.op + " ") + o.text;
    body.appendChild(row);
  }
  entry.argsEl.appendChild(body);
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
  entry.statusEl.className = "tool-status " + (isError ? "error" : "done");
  entry.statusEl.textContent = "";   // 完成后只用一个色点表示状态
  // 不再自动改 collapsed：默认就收起，用户想看详情自行点击展开（出错用红点提示）
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
  // 禁用按钮 + 隐藏整个 actions 区（hidden 配 CSS [hidden] 规则才会真消失）
  card.querySelectorAll(".perm-btn").forEach(b => b.disabled = true);
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
    if (evt.subtype === "backlog_done") {
      state.firstSeq = evt.first_seq;
      state.hasMoreHistory = !!evt.has_more;
      // 每次 backlog 完成都强制贴底：覆盖首次进入 + ws 重连两种 case
      const log = $("chat-log");
      setScrollTopInstant(log, log.scrollHeight);
      requestAnimationFrame(() => setScrollTopInstant(log, log.scrollHeight));
      if (state.revealChat) state.revealChat();
      state.pendingScrollToBottomOnBacklog = false;
      return;
    }
    if (evt.subtype === "permission_request") return showPermissionRequest(evt);
    if (evt.subtype === "permission_resolved") return markPermissionResolved(evt);
    return;
  }
  if (t === "_internal") {
    if (evt.subtype === "exit") setStatus("error", `已退出 rc=${evt.returncode}`);
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
        renderMDIntoBubble(msg.bubble, msg.text);
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
        renderMDIntoBubble(cur.bubble, b.text);
      } else if (b.text) {
        const bubble = appendBubble("assistant", "");
        renderMDIntoBubble(bubble, b.text);
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
  const c = evt.content;
  if (typeof c === "string") {
    appendBubble("user", c);
    return;
  }
  if (!Array.isArray(c)) return;
  const bubble = appendBubble("user", "");
  bubble.textContent = "";
  for (const block of c) {
    if (block && block.type === "image" && block.source && block.source.data) {
      const img = document.createElement("img");
      img.src = `data:${block.source.media_type || "image/png"};base64,${block.source.data}`;
      img.className = "att-thumb-bubble";
      bubble.appendChild(img);
    } else if (block && block.type === "text" && block.text) {
      const span = document.createElement("div");
      span.textContent = block.text;
      bubble.appendChild(span);
    }
  }
}

function handleSystem(evt) {
  if (evt.subtype === "init") {
    setStatus("busy", "已就绪");
  } else if (evt.subtype === "post_turn_summary") {
    setStatus("", "空闲");
  }
  // init/result 的 model/cwd/cost 不再灌到聊天流；chat-meta 已显示 cwd，状态条显示忙闲
}

function handleResult(evt) {
  setStatus("", "完成");
}

function sendUserMessage() {
  const ta = $("chat-input");
  const text = ta.value.trim();
  if ((!text && state.attachments.length === 0)
      || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  if (state.attachments.some(a => a.uploading)) {
    alert("有文件正在上传，请稍候");
    return;
  }
  const images = state.attachments.filter(a => a.kind === "image");
  const files  = state.attachments.filter(a => a.kind === "file" && a.path);
  // 文件附件以路径形式拼到文本里，Claude 会自行 Read
  let combinedText = text;
  if (files.length) {
    const lines = files.map(f => `📎 ${f.name}\n${f.path}`).join("\n\n");
    combinedText = text ? `${text}\n\n${lines}` : lines;
  }
  let content;
  if (images.length) {
    content = images.map(a => ({
      type: "image",
      source: { type: "base64", media_type: a.media_type, data: a.base64 },
    }));
    if (combinedText) content.push({ type: "text", text: combinedText });
  } else {
    content = combinedText;
  }
  state.ws.send(JSON.stringify({ type: "user_message", content }));
  // user bubble 等 server 注入 user_input echo 时再渲染（保证刷新/resume 也能看到）
  ta.value = "";
  ta.style.height = "auto";
  clearAttachments();
  setStatus("busy", "等待回复…");
}

// ---------- 附件 ----------
function clearAttachments() {
  state.attachments = [];
  renderAttachmentBar();
}

function renderAttachmentBar() {
  const bar = $("chat-attachments");
  bar.innerHTML = "";
  if (state.attachments.length === 0) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  state.attachments.forEach((a, idx) => {
    const card = document.createElement("div");
    card.className = "att-card";
    if (a.kind === "image") {
      const img = document.createElement("img");
      img.src = a.dataUrl;
      img.className = "att-thumb";
      card.appendChild(img);
    } else {
      const s = document.createElement("span");
      s.textContent = (a.uploading ? "⏳ " : "📎 ") + (a.name || a.label || "(附件)");
      card.appendChild(s);
      if (a.uploading) card.classList.add("uploading");
    }
    const x = document.createElement("button");
    x.className = "att-x";
    x.type = "button";
    x.textContent = "✕";
    x.addEventListener("click", () => {
      state.attachments.splice(idx, 1);
      renderAttachmentBar();
    });
    card.appendChild(x);
    bar.appendChild(card);
  });
}

function addImageAttachment(file) {
  const reader = new FileReader();
  reader.onload = () => {
    const dataUrl = reader.result;
    // 拆 data:<mime>;base64,<b64>
    const m = /^data:([^;]+);base64,(.+)$/.exec(dataUrl);
    if (!m) return;
    state.attachments.push({
      kind: "image",
      media_type: m[1],
      base64: m[2],
      dataUrl,
    });
    renderAttachmentBar();
  };
  reader.readAsDataURL(file);
}

// 文件处理路由：图片 → image attachment；小文本 → 追加到输入框；其它 → 上传到 session.cwd 作为 file attachment
function handleSelectedFile(f) {
  if (f.type.startsWith("image/")) {
    addImageAttachment(f);
    return;
  }
  const isTextLike = f.type.startsWith("text/")
    || /\.(txt|md|py|js|ts|tsx|jsx|json|html|css|sh|yml|yaml|toml|ini|conf|c|cc|cpp|h|hpp|rs|go|java|kt|swift|log|csv|xml)$/i.test(f.name);
  if (isTextLike && f.size < 200 * 1024) {
    const r = new FileReader();
    r.onload = () => {
      const ta = $("chat-input");
      ta.value += (ta.value ? "\n\n" : "") + "// " + f.name + "\n" + r.result;
      ta.dispatchEvent(new Event("input"));
    };
    r.readAsText(f);
    return;
  }
  uploadFileAttachment(f);
}

async function uploadFileAttachment(f) {
  if (!state.sessionId) { alert("请先打开一个 session"); return; }
  const slot = { kind: "file", name: f.name, size: f.size, uploading: true };
  state.attachments.push(slot);
  renderAttachmentBar();
  try {
    const fd = new FormData();
    fd.append("file", f);
    const headers = state.token ? { "Authorization": "Bearer " + state.token } : {};
    const res = await fetch(
      apiPath(`/api/sessions/${encodeURIComponent(state.sessionId)}/upload`),
      { method: "POST", headers, body: fd },
    );
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(`${res.status} ${detail}`);
    }
    const data = await res.json();
    slot.path = data.path;
    slot.uploading = false;
    renderAttachmentBar();
  } catch (e) {
    const idx = state.attachments.indexOf(slot);
    if (idx >= 0) state.attachments.splice(idx, 1);
    renderAttachmentBar();
    alert(`上传失败：${e.message}`);
  }
}

function setupAttachmentInput() {
  const ta = $("chat-input");
  // 附件按钮 → 隐藏 input（不限类型，回调里统一分类）
  const attInput = $("att-input");
  $("chat-att").addEventListener("click", () => attInput.click());
  attInput.addEventListener("change", e => {
    for (const f of e.target.files) handleSelectedFile(f);
    e.target.value = "";   // 允许同一文件再选一次
  });
  // 粘贴：任意类型的文件都接（图片、PDF、文本…），交给同一个路由
  ta.addEventListener("paste", (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    let handled = false;
    for (const it of items) {
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) { handleSelectedFile(f); handled = true; }
      }
    }
    if (handled) e.preventDefault();
  });
  // 拖拽到整个 chat 视图
  const dropZone = document.querySelector("#view-chat");
  ["dragover", "dragenter"].forEach(ev => {
    dropZone.addEventListener(ev, (e) => {
      if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) {
        e.preventDefault();
        dropZone.classList.add("drag-over");
      }
    });
  });
  ["dragleave", "drop"].forEach(ev => {
    dropZone.addEventListener(ev, () => dropZone.classList.remove("drag-over"));
  });
  dropZone.addEventListener("drop", (e) => {
    if (!e.dataTransfer) return;
    const files = Array.from(e.dataTransfer.files || []);
    if (!files.length) return;
    e.preventDefault();
    for (const f of files) handleSelectedFile(f);
  });
}

$("chat-send").addEventListener("click", sendUserMessage);
$("chat-input").addEventListener("keydown", e => {
  // IME 候选 / 拼写阶段按 Enter 是上屏确认，不应触发发送：
  //   - e.isComposing: 现代浏览器
  //   - keyCode === 229: 仅 Safari 旧版兜底（isComposing 不可靠时）
  if (e.isComposing || e.keyCode === 229) return;
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
setupAttachmentInput();
// 回到前台 / 网络恢复时立即重连两条 ws：iOS 切换 app 后 ws 通常已被运营商或 iOS 关掉，
// 但 onclose 可能延迟触发，等指数退避要好久。这里主动 poke 一下。
function kickReconnect() {
  if (!state.token) return;
  // 全局 ws
  if (!state.globalWS
      || state.globalWS.readyState === WebSocket.CLOSED
      || state.globalWS.readyState === WebSocket.CLOSING) {
    _globalBackoff = 1000;   // 重置退避
    connectGlobalWS();
  }
  // 当前 session 的 chat ws
  if (state.sessionId && state.reconnectChatNow) state.reconnectChatNow();
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") kickReconnect();
});
window.addEventListener("online", kickReconnect);
window.addEventListener("pageshow", () => kickReconnect());   // 从 bfcache 唤回也算
// 主题切换按钮
document.querySelectorAll("#theme-toggle, #theme-toggle-login").forEach(b => {
  b.addEventListener("click", toggleTheme);
});
// PWA service worker（只在 secure context 下有效；http 公网会静默失败，不影响功能）
// register 路径基于 <base href>，scope 同前缀
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    const swURL = new URL("sw.js", document.baseURI).pathname;
    const swScope = new URL("./", document.baseURI).pathname;
    navigator.serviceWorker.register(swURL, { scope: swScope })
      .catch(err => console.warn("SW register failed (expected on http):", err.message));
  });
}
if (state.token) {
  tryLogin(state.token).then(enterHome).catch(() => {
    state.token = "";
    localStorage.removeItem("ccr.token");
    showView("login");
  });
} else {
  showView("login");
}
