// ClaudeCodeRemote 前端：登录 → 会话列表 → 单会话聊天。
// M2: 工具调用卡片渲染 + tool_result 配对 + 流式参数累积。

const __CCR_APP_VER = "v173";

const $ = (id) => document.getElementById(id);

const state = {
  token: localStorage.getItem("ccr.token") || "",
  cwd: localStorage.getItem("ccr.cwd") || "",
  // M-Hub-4: SPA probe 后由 /api/me 设. hubMode=true → 走 cookie auth +
  // session card 显 app chip + spawn modal 显 app selector. 默认 local 不变.
  hubMode: false,
  userId: null,
  apps: [],
  ws: null,
  sessionId: null,
  // 流式状态：
  msgById: new Map(),         // msg_id -> {bubble, text}
  toolById: new Map(),         // tool_use_id -> {card, partialInput, finalInput, resultEl}
  askuserById: new Map(),      // tool_use_id -> {card, questions, answers, submitted}
  activeMsgId: null,           // 当前打开的 stream message id
  blocksByIdx: new Map(),      // stream message index -> {type, msgId|toolUseId}
  // 每个 session 的 DOM + 流式状态快照：切走时缓存，切回来直接复用，免 spinner 免重渲
  sessionCache: new Map(),     // session_id -> snapshot
  maxSeq: 0,                   // 当前 session 已处理事件的最高 seq；WS 重连 dedupe 用
  // chat-log 是否在底部 (距底 < 40 px). 由 scroll 监听器维护. 决定新消息
  // 到达时 chatScrollBottom() 要不要跟进 — 用户滚走时不能抢阅读位置.
  atBottom: true,
};
const SESSION_CACHE_MAX = 10;

// ---------- IndexedDB: 聊天记录浏览器端缓存 (Step 1-2) ----------
// 目的: 进 session 时立即从 IDB 渲上次缓存内容 (0 latency reveal), 同时
// WS 后台连 → server 推 backlog → dedup (state.maxSeq) → 只补增量.
// schema:
//   ccr/v1/messages  keyPath=['sess_id','seq']  index 'by_sess' on sess_id
const IDB_NAME = "ccr";
const IDB_VERSION = 2;
const IDB_STORE_MESSAGES = "messages";
const IDB_STORE_OUTBOX = "outbox";
const IDB_MAX_PER_SESS = 1000;   // LRU cap per session, oldest by seq dropped

let _idbDb = null;
let _idbOpening = null;
function idbOpen() {
  if (_idbDb) return Promise.resolve(_idbDb);
  if (_idbOpening) return _idbOpening;
  _idbOpening = new Promise((resolve, reject) => {
    if (!("indexedDB" in window)) {
      reject(new Error("IndexedDB unavailable"));
      return;
    }
    const req = indexedDB.open(IDB_NAME, IDB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(IDB_STORE_MESSAGES)) {
        const store = db.createObjectStore(IDB_STORE_MESSAGES, {
          keyPath: ["sess_id", "seq"],
        });
        store.createIndex("by_sess", "sess_id", { unique: false });
      }
      // v2: outbox — 未 ack 的 user_message. keyPath client_msg_id (uuid)
      // index 'by_sess' 用于 enterChat / reconnect 时按 session 列待发.
      if (!db.objectStoreNames.contains(IDB_STORE_OUTBOX)) {
        const obs = db.createObjectStore(IDB_STORE_OUTBOX, {
          keyPath: "client_msg_id",
        });
        obs.createIndex("by_sess", "sess_id", { unique: false });
      }
    };
    req.onsuccess = () => {
      _idbDb = req.result;
      _idbDb.onversionchange = () => { try { _idbDb.close(); } catch (_) {} _idbDb = null; };
      resolve(_idbDb);
    };
    req.onerror = () => reject(req.error);
  });
  return _idbOpening;
}

async function idbGetSessionMessages(sessId) {
  if (!sessId) return [];
  try {
    const db = await idbOpen();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE_MESSAGES, "readonly");
      const store = tx.objectStore(IDB_STORE_MESSAGES);
      const idx = store.index("by_sess");
      const req = idx.getAll(IDBKeyRange.only(sessId));
      req.onsuccess = () => {
        const rows = (req.result || []).slice();
        rows.sort((a, b) => a.seq - b.seq);   // 按 seq 升序回放
        resolve(rows);
      };
      req.onerror = () => reject(req.error);
    });
  } catch (e) {
    console.warn("idbGetSessionMessages failed:", e);
    return [];
  }
}

async function idbPutMessage(sessId, env) {
  // env: {seq, ts, event}. 不 await — 调用方 fire-and-forget.
  if (!sessId || !env || typeof env.seq !== "number") return;
  try {
    const db = await idbOpen();
    const tx = db.transaction(IDB_STORE_MESSAGES, "readwrite");
    const store = tx.objectStore(IDB_STORE_MESSAGES);
    store.put({
      sess_id: sessId,
      seq: env.seq,
      ts: env.ts,
      event: env.event,
    });
  } catch (e) {
    console.warn("idbPutMessage failed:", e);
  }
}

async function idbDeleteSession(sessId) {
  if (!sessId) return;
  try {
    const db = await idbOpen();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE_MESSAGES, "readwrite");
      const store = tx.objectStore(IDB_STORE_MESSAGES);
      const idx = store.index("by_sess");
      const req = idx.openKeyCursor(IDBKeyRange.only(sessId));
      req.onsuccess = (e) => {
        const cur = e.target.result;
        if (cur) {
          store.delete(cur.primaryKey);
          cur.continue();
        } else {
          resolve();
        }
      };
      req.onerror = () => reject(req.error);
    });
  } catch (e) {
    console.warn("idbDeleteSession failed:", e);
  }
}

// 跟 server _classify 同款白名单 — 仅缓存这些 envelope, 排除 transient
// stream_event / message_start / content_block_*.
function _idbWriteKind(evt) {
  if (!evt) return false;
  const t = evt.type;
  if (t === "assistant" || t === "user" || t === "user_input" || t === "result") {
    return true;
  }
  if (t === "system" && evt.subtype === "init") return true;
  if (t === "_ccr") {
    const sub = evt.subtype;
    // first_paint / backlog_done 是 server 用 seq=-1 wrap 的, 已被 seq>0
    // 过滤掉, 这里不列. 列的是真正持久化的 _ccr 子类型.
    return sub === "permission_request" || sub === "permission_resolved"
        || sub === "askuser_request" || sub === "askuser_resolved"
        || sub === "turn_summary";
  }
  return false;
}

// ---------- outbox: 未 ack 的 user_message (Step 3) ----------
// 用户点发送 → 立即写 outbox + ws.send. server 回 user_input event 带回
// client_msg_id → 此时才 delete outbox. WS 断 / reconnect 时扫 outbox 重发.
function _uuid() {
  if (crypto && crypto.randomUUID) return crypto.randomUUID();
  // Fallback for old browsers
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === "x" ? r : (r & 3 | 8)).toString(16);
  });
}

async function idbPutOutbox(entry) {
  // entry: {client_msg_id, sess_id, content, created_at, attempts}
  try {
    const db = await idbOpen();
    const tx = db.transaction(IDB_STORE_OUTBOX, "readwrite");
    tx.objectStore(IDB_STORE_OUTBOX).put(entry);
  } catch (e) {
    console.warn("idbPutOutbox failed:", e);
  }
}

async function idbDeleteOutbox(clientMsgId) {
  if (!clientMsgId) return;
  try {
    const db = await idbOpen();
    const tx = db.transaction(IDB_STORE_OUTBOX, "readwrite");
    tx.objectStore(IDB_STORE_OUTBOX).delete(clientMsgId);
  } catch (e) {
    console.warn("idbDeleteOutbox failed:", e);
  }
}

async function idbListOutboxBySess(sessId) {
  if (!sessId) return [];
  try {
    const db = await idbOpen();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE_OUTBOX, "readonly");
      const idx = tx.objectStore(IDB_STORE_OUTBOX).index("by_sess");
      const req = idx.getAll(IDBKeyRange.only(sessId));
      req.onsuccess = () => {
        const rows = (req.result || []).slice();
        rows.sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
        resolve(rows);
      };
      req.onerror = () => reject(req.error);
    });
  } catch (e) {
    console.warn("idbListOutboxBySess failed:", e);
    return [];
  }
}

// LRU: 超过 IDB_MAX_PER_SESS 时删最老的 (按 seq 升序删头). Step 2 用.
async function idbTrimSession(sessId, cap) {
  cap = cap || IDB_MAX_PER_SESS;
  if (!sessId) return;
  try {
    const db = await idbOpen();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE_MESSAGES, "readwrite");
      const store = tx.objectStore(IDB_STORE_MESSAGES);
      const idx = store.index("by_sess");
      const countReq = idx.count(IDBKeyRange.only(sessId));
      countReq.onsuccess = () => {
        const n = countReq.result;
        if (n <= cap) { resolve(); return; }
        const drop = n - cap;
        const cur = idx.openKeyCursor(IDBKeyRange.only(sessId));   // seq asc
        let removed = 0;
        cur.onsuccess = (e) => {
          const c = e.target.result;
          if (c && removed < drop) {
            store.delete(c.primaryKey);
            removed++;
            c.continue();
          } else {
            resolve();
          }
        };
        cur.onerror = () => reject(cur.error);
      };
      countReq.onerror = () => reject(countReq.error);
    });
  } catch (e) {
    console.warn("idbTrimSession failed:", e);
  }
}

// 把 /home/<user>/... 或 /Users/<user>/... 缩成 ~/... 显示形式.
// 服务器收到 ~ 会用 os.path.expanduser 还原, 所以始终用缩写形式存 +
// 在 UI 里显示, 既好看, 也省一遍前端展开. 留作弊门: 用户手输 / 之类
// 的绝对路径原样保留.
function abbreviateHome(path) {
  const p = (path || "").trim();
  if (!p) return p;
  return p.replace(/^\/home\/[^/]+/, "~").replace(/^\/Users\/[^/]+/, "~");
}

// Most-recent-used cwds for the chip strip. Stored as a JSON array in
// localStorage.ccr.recentCwds, left = newest, max 10 entries. Updated on
// every successful spawn (see spawn handler).
const RECENT_CWDS_KEY = "ccr.recentCwds";
const RECENT_CWDS_MAX = 10;
function loadRecentCwds() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_CWDS_KEY) || "[]");
    // 老数据可能是绝对路径; 读出来时统一缩写, 保持显示一致.
    return Array.isArray(v)
      ? v.filter(x => typeof x === "string" && x).map(abbreviateHome)
      : [];
  } catch (e) { return []; }
}
function pushRecentCwd(path) {
  const p = abbreviateHome((path || "").trim());
  if (!p) return;
  const list = loadRecentCwds().filter(x => x !== p);
  list.unshift(p);
  list.length = Math.min(list.length, RECENT_CWDS_MAX);
  try { localStorage.setItem(RECENT_CWDS_KEY, JSON.stringify(list)); } catch (e) {}
  renderPresets();
}

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

// ---------- PWA / Safari tab 检测 ----------
// standalone display-mode: 标准 (Android Chrome + iOS Safari 16+);
// navigator.standalone: iOS 专属兜底.
const _IS_PWA = window.matchMedia("(display-mode: standalone)").matches
             || window.navigator.standalone === true;
if (_IS_PWA) document.body.classList.add("is-pwa");

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
  // local mode 用 bearer token; hub mode 走 cookie (credentials: include).
  if (!state.hubMode && state.token) {
    headers["Authorization"] = "Bearer " + state.token;
  }
  const res = await fetch(apiPath(path), {
    ...opts, headers,
    credentials: state.hubMode ? "include" : "same-origin",
  });
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

// 渲染 login 页底部的 OAuth 按钮 (Google / GitHub / Gitee / ...).
// state.oauthProviders 由 boot() /api/me 拿到. 没启用 provider 时啥都不渲.
function renderOAuthButtons() {
  const box = $("login-oauth");
  if (!box) return;
  box.innerHTML = "";
  const list = state.oauthProviders || [];
  if (!list.length) return;
  const row = document.createElement("div");
  row.className = "login-oauth-row";
  for (const p of list) {
    const a = document.createElement("a");
    a.href = `api/hub/auth/${encodeURIComponent(p.key)}/start`;
    a.className = "login-oauth-iconbtn";
    a.title = `Sign in with ${p.label || p.key}`;
    a.setAttribute("aria-label", a.title);
    const base = new URL("./", document.baseURI).pathname;
    a.innerHTML = `<img src="${base}static/lib/oauth-icons/${encodeURIComponent(p.key)}.svg" alt="${escHTML(p.label || p.key)}" width="22" height="22">`;
    row.appendChild(a);
  }
  box.appendChild(row);
}

async function hubLogin(email, password) {
  // hub mode: POST /api/hub/login → server set ccr_sess cookie
  await api("/api/hub/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  // 重新探一次 me 同步 userId + apps
  const me = await api("/api/me");
  state.userId = me.user_id;
  state.apps = me.apps || [];
}

$("login-go").addEventListener("click", async () => {
  $("login-err").classList.remove("show");
  if (state.hubMode) {
    const email = ($("login-email").value || "").trim();
    const pw = $("login-password").value || "";
    if (!email || !pw) {
      $("login-err").textContent = "Email + password required";
      $("login-err").classList.add("show");
      return;
    }
    try {
      await hubLogin(email, pw);
      enterHomeOrOnboarding();
    } catch (e) {
      $("login-err").textContent = "Sign in failed: " + (e.message || e);
      $("login-err").classList.add("show");
    }
    return;
  }
  const tok = $("login-token").value.trim();
  if (!tok) {
    $("login-err").textContent = "Token required";
    $("login-err").classList.add("show");
    return;
  }
  try {
    await tryLogin(tok);
    enterHome();
  } catch (e) {
    $("login-err").textContent = "Sign in failed: " + (e.message || e);
    $("login-err").classList.add("show");
  }
});
$("login-token").addEventListener("keydown", e => {
  if (e.key === "Enter") $("login-go").click();
});
["login-email", "login-password"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("keydown", e => {
    if (e.key === "Enter") $("login-go").click();
  });
});

$("logout").addEventListener("click", async (e) => {
  e.preventDefault();
  if (state.hubMode) {
    try { await api("/api/hub/logout", { method: "POST" }); } catch (_) {}
    state.userId = null;
    state.apps = [];
    state.sessionId = null;
    showView("login");
    return;
  }
  state.token = "";
  state.sessionId = null;
  localStorage.removeItem("ccr.token");
  document.body.classList.remove("has-session");
  showView("login");
});

const _hardReloadEl = $("hard-reload");
if (_hardReloadEl) _hardReloadEl.textContent = __CCR_APP_VER;
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
  for (const path of loadRecentCwds()) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.dataset.path = path;
    b.textContent = path;
    b.title = path;
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
    c.classList.toggle("active", c.dataset.path === v && v !== "");
  });
}
$("spawn-cwd").addEventListener("input", syncPresetChips);

// ---------- 目录浏览 modal ----------
const _browse = { curPath: "" };
// hub mode: ls/mkdir 必须按用户当前在 spawn-app select 里选的 server
// 路由, 否则 path browser 串台. local mode 返空.
function _browseTargetAppId() {
  if (!state.hubMode) return "";
  const sel = $("spawn-app");
  return (sel && sel.value) || "";
}
async function browseLoad(path) {
  const list = $("modal-list");
  list.innerHTML = '<div class="modal-empty">Loading…</div>';
  try {
    const appId = _browseTargetAppId();
    const qs = [`path=${encodeURIComponent(path || "")}`];
    if (appId) qs.push(`app_id=${encodeURIComponent(appId)}`);
    const j = await api(`api/ls?${qs.join("&")}`);
    _browse.curPath = j.path;
    $("modal-crumb").textContent = abbreviateHome(j.path);
    const rows = [];
    if (j.parent !== null) {
      rows.push(`<div class="modal-row parent" data-path="${escHTML(j.parent)}"><span class="icon">↰</span><span class="name">.. (parent)</span></div>`);
    }
    for (const d of j.dirs) {
      const child = j.path === "/" ? "/" + d : j.path + "/" + d;
      rows.push(`<div class="modal-row" data-path="${escHTML(child)}"><span class="icon">📁</span><span class="name">${escHTML(d)}</span></div>`);
    }
    if (!j.dirs.length) {
      rows.push('<div class="modal-empty">empty</div>');
    }
    list.innerHTML = rows.join("");
    list.querySelectorAll(".modal-row").forEach(el => {
      el.addEventListener("click", () => browseLoad(el.dataset.path));
    });
  } catch (e) {
    list.innerHTML = `<div class="modal-empty err show">Load failed: ${escHTML(e.message)}</div>`;
  }
}
let _browseTargetId = "spawn-cwd";
function openBrowse(targetId = "spawn-cwd") {
  _browseTargetId = targetId;
  $("modal-browse").hidden = false;
  browseLoad($(_browseTargetId).value.trim() || "~");
}
function closeBrowse() {
  $("modal-browse").hidden = true;
}
$("browse-btn").addEventListener("click", () => openBrowse("spawn-cwd"));
$("modal-close").addEventListener("click", closeBrowse);
$("modal-cancel").addEventListener("click", closeBrowse);
$("modal-confirm").addEventListener("click", () => {
  if (_browse.curPath) {
    // 写到输入框前缩写成 ~/... 让 UI 显示干净 (服务端 expanduser 还原)
    const display = abbreviateHome(_browse.curPath);
    const target = $(_browseTargetId);
    if (target) target.value = display;
    if (_browseTargetId === "spawn-cwd") syncPresetChips();
  }
  closeBrowse();
});
$("modal-browse").addEventListener("click", (e) => {
  if (e.target.id === "modal-browse") closeBrowse();
});
$("modal-newdir").addEventListener("click", async () => {
  const parent = _browse.curPath;
  if (!parent) return;
  const name = (prompt(`Create new folder in:\n${parent}\n\nName:`) || "").trim();
  if (!name) return;
  try {
    const appId = _browseTargetAppId();
    const url = appId
      ? `api/mkdir?app_id=${encodeURIComponent(appId)}`
      : "api/mkdir";
    const r = await api(url, {
      method: "POST",
      body: JSON.stringify({ parent, name }),
    });
    // 刷新当前目录，新文件夹会自动出现在列表里；同时把 curPath 跳进去更方便用户继续
    await browseLoad(r.path);
  } catch (e) {
    alert("New folder failed: " + (e.message || e));
  }
});

const STATE_BADGES = {
  busy:                { label: "Busy",          cls: "busy" },
  waiting_permission:  { label: "Needs approval", cls: "waiting" },
  needs_input:         { label: "Needs input",   cls: "needs-input" },
  idle:                { label: "Idle",          cls: "idle" },
  hibernated:          { label: "Hibernated",    cls: "hibernated" },
  finished:            { label: "Finished",      cls: "finished" },
};

// 主页状态板：按 sess.id 缓存当前快照
state.sessionsById = new Map();   // id -> session payload
state.globalWS = null;
state.attachments = [];           // 待发送附件列表：[{kind, media_type, base64, dataUrl}]

function relTime(ts) {
  if (!ts) return "";
  const d = Date.now()/1000 - ts;
  if (d < 60)     return Math.floor(d) + "s";
  if (d < 3600)   return Math.floor(d/60) + "m";
  if (d < 86400)  return Math.floor(d/3600) + "h";
  return Math.floor(d/86400) + "d";
}

function getSortMode() {
  return localStorage.getItem("ccr.sortMode") || "created";
}
function setSortMode(mode) {
  try { localStorage.setItem("ccr.sortMode", mode); } catch (e) {}
  const btn = $("sessions-sort");
  if (btn) {
    btn.dataset.mode = mode;
    // Tooltip explains current mode + what next click does
    btn.title = mode === "created"
      ? "Sorted by creation date — click for last-active"
      : "Sorted by last-active — click for creation date";
  }
}
function renderSessionList() {
  const listActive   = $("session-list-active");
  const listStash    = $("session-list-stash");
  const listInactive = $("session-list-inactive");
  const activeBox    = $("sessions-active");
  const stashBox     = $("sessions-stash");
  const inactiveBox  = $("sessions-inactive");
  const mode = getSortMode();
  const all = Array.from(state.sessionsById.values()).sort((a, b) => {
    // active mode uses the hysteresis snapshot (_sortKey) so small bumps
    // in last_activity_at (≤ 180 s) don't reshuffle the list.
    const ka = mode === "active"
      ? ((typeof a._sortKey === "number") ? a._sortKey : (a.last_activity_at || 0))
      : (a.created_at || 0);
    const kb = mode === "active"
      ? ((typeof b._sortKey === "number") ? b._sortKey : (b.last_activity_at || 0))
      : (b.created_at || 0);
    return kb - ka;
  });
  // Three mutually-exclusive buckets. Stash sits between Active and
  // Inactive. is_stash and is_inactive are mutually exclusive server-side.
  const active   = all.filter(s => !s.is_inactive && !s.is_stash);
  const stash    = all.filter(s =>  s.is_stash);
  const inactive = all.filter(s =>  s.is_inactive);

  // Section headers always visible; count is empty when 0.
  activeBox.querySelector(".count").textContent = active.length ? `(${active.length})` : "";
  stashBox.querySelector(".count").textContent = stash.length ? `(${stash.length})` : "";
  inactiveBox.querySelector(".count").textContent = inactive.length ? `(${inactive.length})` : "";

  // Render active
  if (!active.length) {
    listActive.innerHTML = `<div class="session-empty">No sessions</div>`;
  } else {
    listActive.innerHTML = "";
    for (const s of active) renderOneCard(s, listActive, /*section=*/"active");
  }
  listStash.innerHTML = "";
  for (const s of stash) renderOneCard(s, listStash, /*section=*/"stash");
  listInactive.innerHTML = "";
  for (const s of inactive) renderOneCard(s, listInactive, /*section=*/"inactive");
}

function renderOneCard(s, container, section) {
  // section: "active" | "stash" | "inactive"
  // Legacy callers passed a boolean isInactiveSection; map that here so
  // any straggler still works.
  if (typeof section === "boolean") section = section ? "inactive" : "active";
  const badge = STATE_BADGES[s.state] || STATE_BADGES.idle;
  const active = relTime(s.last_activity_at);
  const pp = s.pending_permissions || 0;
  const needs = s.needs_action_detail;
  const isCurrent = state.sessionId === s.id;
  const isBusy = s.state === "busy";
  // Only render a text badge when the user is expected to act. Busy /
  // idle / hibernated / finished get only the state-dot + (for busy)
  // the green-glow card border — no text label.
  const showBadge = s.state === "waiting_permission"
                 || s.state === "needs_input";
  const el = document.createElement("div");
  el.className = "session-card state-" + (badge.cls || "idle")
               + (isBusy ? " session-busy" : "")
               + (isCurrent ? " is-current" : "");
  el.setAttribute("data-id", s.id);
  // Absolute path, but $HOME → "~". Detect /home/<user> and /Users/<user>
  // prefixes since we don't have access to the actual env from the browser.
  let cwdShort = (s.cwd || "");
  cwdShort = cwdShort
    .replace(/^\/home\/[^/]+/, "~")
    .replace(/^\/Users\/[^/]+/, "~");
  const badgeLabel = badge.label + (pp > 1 ? ` ×${pp}` : "");
  // Top-right kebab menu. Items per section:
  //   Active   → Rename / Stash / Deactivate / Delete
  //   Stash    → Rename / Activate / Deactivate / Delete
  //   Inactive → Rename / Activate / Stash / Delete
  let menuItemsHtml;
  if (section === "active") {
    menuItemsHtml = `
      <button class="card-menu-item" role="menuitem" data-action="rename">Rename</button>
      <button class="card-menu-item" role="menuitem" data-action="stash">Stash</button>
      <button class="card-menu-item" role="menuitem" data-action="deactivate">Deactivate</button>
      <button class="card-menu-item card-menu-item-danger" role="menuitem" data-action="delete">Delete</button>`;
  } else if (section === "stash") {
    menuItemsHtml = `
      <button class="card-menu-item" role="menuitem" data-action="rename">Rename</button>
      <button class="card-menu-item" role="menuitem" data-action="activate">Activate</button>
      <button class="card-menu-item" role="menuitem" data-action="deactivate">Deactivate</button>
      <button class="card-menu-item card-menu-item-danger" role="menuitem" data-action="delete">Delete</button>`;
  } else {
    // inactive: Rename / Activate / Stash / Delete
    menuItemsHtml = `
      <button class="card-menu-item" role="menuitem" data-action="rename">Rename</button>
      <button class="card-menu-item" role="menuitem" data-action="activate">Activate</button>
      <button class="card-menu-item" role="menuitem" data-action="stash">Stash</button>
      <button class="card-menu-item card-menu-item-danger" role="menuitem" data-action="delete">Delete</button>`;
  }
  // hub mode: 加 app chip 显示 session 归属的 app + online 状态. local 模式
  // CSS 用 display:none 隐藏, 不渲染分支也行.
  const appChipHTML = (state.hubMode && s.app_name) ? (
    `<span class="app-chip ${s.app_online ? "" : "offline"}"
            title="${escHTML(s.app_name)} (${s.app_online ? "online" : "offline"})">
       ${escHTML(s.app_name)}
     </span>`
  ) : "";
  el.innerHTML = `
    <button class="card-menu-btn" aria-label="More" title="More">⋯</button>
    <div class="card-menu" hidden role="menu">${menuItemsHtml}</div>
    <div class="session-row1">
      <span class="state-dot" aria-hidden="true"></span>
      <div class="name">${escHTML(s.name || "untitled")}</div>
      ${showBadge ? `<span class="badge ${badge.cls}">${escHTML(badgeLabel)}</span>` : ""}
      ${appChipHTML}
    </div>
    <div class="meta-line">
      <span class="cwd-short" dir="rtl"><bdo dir="ltr">${escHTML(cwdShort)}</bdo></span>
      <span class="ts">${escHTML(active)} ago</span>
    </div>`;

  const menuBtn  = el.querySelector(".card-menu-btn");
  const menu     = el.querySelector(".card-menu");

  function openMenu() {
    document.querySelectorAll(".card-menu:not([hidden])").forEach(m => {
      if (m !== menu) m.setAttribute("hidden", "");
    });
    menu.removeAttribute("hidden");
  }
  function closeMenu() { menu.setAttribute("hidden", ""); }

  function startRename() {
    // Edit the .name div in-place via contenteditable — same element,
    // same box dimensions, zero layout shift on enter/exit.
    const nameEl = el.querySelector(".name");
    if (!nameEl || nameEl.classList.contains("editing")) return;
    const original = s.name || "untitled";
    // Defensively reset textContent — if the rendered ellipsis state
    // ever leaked into the DOM (some old browsers did this on
    // contenteditable activation), this restores the canonical name.
    nameEl.textContent = original;
    nameEl.contentEditable = "true";
    nameEl.spellcheck = false;
    nameEl.classList.add("editing");
    nameEl.focus();
    // Select all of the existing text
    const range = document.createRange();
    range.selectNodeContents(nameEl);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);

    let settled = false;
    function leaveEdit() {
      nameEl.contentEditable = "false";
      nameEl.classList.remove("editing");
      nameEl.removeEventListener("keydown", onKey);
      nameEl.removeEventListener("blur", onBlur);
    }
    async function commit() {
      if (settled) return;
      settled = true;
      const newName = (nameEl.textContent || "").trim();
      leaveEdit();
      if (!newName || newName === original) {
        nameEl.textContent = original;
        return;
      }
      nameEl.textContent = newName;   // optimistic
      // Patch the state map so a fresh re-render uses the new name.
      const stateSess = state.sessionsById.get(s.id);
      if (stateSess) stateSess.name = newName;
      // Skip the WS-echo full-list rebuild — we'll do a clean single-card
      // re-render below once the server confirms, which avoids the blank
      // moment but still gives us a fresh DOM with correct layout (iOS
      // Safari caches stale intrinsic widths after contenteditable exits).
      renameInFlight.add(s.id);
      try {
        await api(`/api/sessions/${encodeURIComponent(s.id)}/rename`,
                   { method: "PUT", body: JSON.stringify({ name: newName }) });
        s.name = newName;
        rerenderOneCardInPlace(s.id);
      } catch (err) {
        alert("Rename failed: " + err.message);
        nameEl.textContent = original;
      } finally {
        setTimeout(() => renameInFlight.delete(s.id), 250);
      }
    }
    function cancel() {
      if (settled) return;
      settled = true;
      nameEl.textContent = original;
      leaveEdit();
    }
    function onKey(e) {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      else if (e.key === "Escape") { e.preventDefault(); cancel(); }
    }
    function onBlur() { commit(); }
    nameEl.addEventListener("keydown", onKey);
    nameEl.addEventListener("blur", onBlur);
    nameEl.addEventListener("click", e => e.stopPropagation());
  }

  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.hasAttribute("hidden")) openMenu(); else closeMenu();
  });
  menu.querySelectorAll(".card-menu-item").forEach(item => {
    item.addEventListener("click", async (e) => {
      e.stopPropagation();
      closeMenu();
      const action = item.dataset.action;
      if (action === "rename") {
        startRename();
      } else if (action === "delete") {
        if (!confirm(`Delete session "${s.name}"? This cannot be undone.`)) return;
        try {
          await api(`/api/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
          idbDeleteSession(s.id);   // 顺便清掉浏览器 IDB 缓存
        } catch (err) { alert("Delete failed: " + err.message); }
      } else if (action === "deactivate") {
        _optimisticSessionUpdate(s.id, { is_inactive: true, is_stash: false });
        try {
          await api(`/api/sessions/${encodeURIComponent(s.id)}/deactivate`,
                     { method: "POST", body: JSON.stringify({}) });
          if (state.hubMode) setTimeout(hubFetchSessions, 500);
        } catch (err) {
          alert("Deactivate failed: " + err.message);
          if (state.hubMode) hubFetchSessions();   // 回滚
        }
      } else if (action === "stash") {
        _optimisticSessionUpdate(s.id, { is_stash: true, is_inactive: false });
        try {
          await api(`/api/sessions/${encodeURIComponent(s.id)}/stash`,
                     { method: "POST", body: JSON.stringify({}) });
          if (state.hubMode) setTimeout(hubFetchSessions, 500);
        } catch (err) {
          alert("Stash failed: " + err.message);
          if (state.hubMode) hubFetchSessions();
        }
      } else if (action === "activate") {
        _optimisticSessionUpdate(s.id, { is_inactive: false, is_stash: false });
        try {
          await api(`/api/sessions/${encodeURIComponent(s.id)}/activate`,
                     { method: "POST", body: JSON.stringify({}) });
          if (state.hubMode) setTimeout(hubFetchSessions, 500);
        } catch (err) {
          alert("Activate failed: " + err.message);
          if (state.hubMode) hubFetchSessions();
        }
      }
    });
  });

  el.addEventListener("click", () => {
    // 用户在卡片里拖选文字时, mouseup 也会触发 click, 然后切到 chat ——
    // 让选中文本变得不可能. 这里检测是否存在非空选区, 有就不导航.
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.toString().length > 0) return;
    // 如果这次点击的 mousedown / touchstart 刚好关掉了一个 open card-menu,
    // 这次 click 只算"关菜单", 不应导航到卡片.
    if (_cardMenuJustClosed) return;
    if (state.sessionId === s.id) return;
    enterChat(s.id, s.name, s.cwd, s.state);
  });
  container.appendChild(el);

  // Attach overflow-only tooltips: title appears only when the text was
  // actually truncated (scrollWidth > clientWidth). Defer to next frame
  // so layout has settled.
  const nameEl = el.querySelector(".name");
  const cwdEl  = el.querySelector(".cwd-short");
  requestAnimationFrame(() => {
    setTitleIfClipped(nameEl, s.name || "untitled");
    setTitleIfClipped(cwdEl, s.cwd || "");
  });
}

function setTitleIfClipped(el, fullText) {
  if (!el || !el.isConnected) return;
  if (el.scrollWidth > el.clientWidth + 1) {
    el.title = fullText;
  } else {
    el.removeAttribute("title");
  }
}

// Replace a single .session-card in place with a freshly-rendered one
// (using current state.sessionsById). Used after rename commit so iOS
// Safari can't keep a stale intrinsic-width from the contenteditable
// session. Lighter than renderSessionList() — no full innerHTML reset,
// only one card flips.
function rerenderOneCardInPlace(sid) {
  const old = document.querySelector(`.session-card[data-id="${CSS.escape(sid)}"]`);
  if (!old || !old.parentNode) return;
  const sess = state.sessionsById.get(sid);
  if (!sess) return;
  const container = old.parentNode;
  const isInactiveSection = container.id === "session-list-inactive";
  // renderOneCard appends to container; remember the position to slot
  // the new card into and then move it to where the old one was.
  const placeholder = document.createComment("rerender-slot");
  old.replaceWith(placeholder);
  renderOneCard(sess, container, isInactiveSection);
  const fresh = container.lastElementChild;
  if (fresh) placeholder.replaceWith(fresh);
  else placeholder.remove();
}

// Re-evaluate clipping on window resize so a card that newly truncates
// (or stops truncating) keeps its title attribute in sync.
if (!window.__cardTooltipResizeBound) {
  window.__cardTooltipResizeBound = true;
  window.addEventListener("resize", () => {
    document.querySelectorAll(".session-card").forEach(card => {
      const sid = card.getAttribute("data-id");
      const s = sid && state.sessionsById.get(sid);
      if (!s) return;
      setTitleIfClipped(card.querySelector(".name"), s.name || "untitled");
      setTitleIfClipped(card.querySelector(".cwd-short"), s.cwd || "");
    });
  }, { passive: true });
}

// Close any open card-menu when tapping elsewhere (registered once).
// 同时记录这次 down/start 是否关掉了至少一个菜单 — 紧跟着 mouseup / 合成
// click 触发的 card click 必须吞掉, 否则用户摸到背后的卡片就会被误导航进去.
//
// 桌面: mousedown → click 同一 event-loop turn, 0ms 即可清.
// 触屏: touchstart → touchend → 合成 click 跨多个 turn, 用 500ms 兜底 +
//       documnet 的 bubble-phase click 即时清 (card 的 click 是 bubble 阶段,
//       document 比 card 后到, 所以 card handler 能先读到 flag).
let _cardMenuJustClosed = false;
let _cardMenuClearTimer = null;
function _armCardMenuClosedFlag() {
  _cardMenuJustClosed = true;
  if (_cardMenuClearTimer) clearTimeout(_cardMenuClearTimer);
  _cardMenuClearTimer = setTimeout(() => {
    _cardMenuJustClosed = false;
    _cardMenuClearTimer = null;
  }, 500);
}
function _maybeCloseMenusForTap(target) {
  let closedAny = false;
  document.querySelectorAll(".card-menu:not([hidden])").forEach(m => {
    if (!m.contains(target) && !m.previousElementSibling?.contains(target)) {
      m.setAttribute("hidden", "");
      closedAny = true;
    }
  });
  if (closedAny) _armCardMenuClosedFlag();
}
if (!window.__cardMenuCloseBound) {
  window.__cardMenuCloseBound = true;
  document.addEventListener("mousedown", (e) => {
    _maybeCloseMenusForTap(e.target);
  }, true);
  document.addEventListener("touchstart", (e) => {
    _maybeCloseMenusForTap(e.target);
  }, { passive: true, capture: true });
  // 在 click 完成 bubble 之后清 flag — card 的 click handler 是 bubble,
  // document 比 card 后到, 这条不会抢在 card 之前清.
  document.addEventListener("click", () => {
    if (_cardMenuJustClosed) {
      _cardMenuJustClosed = false;
      if (_cardMenuClearTimer) {
        clearTimeout(_cardMenuClearTimer);
        _cardMenuClearTimer = null;
      }
    }
  });
}

// ---------- Inactive section collapse toggle ----------
// 用户偏好: 每次加载都是收起状态, 不持久化 — 防止"上次我打开了, 这次还
// 张着"的状态意外保留. 折叠/展开仅在本次会话内有效.
(function setupInactiveToggle() {
  const box = $("sessions-inactive");
  if (!box) return;
  const header = box.querySelector(".inactive-toggle");
  if (!header) return;
  header.addEventListener("click", () => {
    box.classList.toggle("expanded");
  });
})();

// ---------- Stash section collapse toggle (default expanded) ----------
(function setupStashToggle() {
  const box = $("sessions-stash");
  if (!box) return;
  const header = box.querySelector(".stash-toggle");
  if (!header) return;
  // Default expanded; collapse only if user explicitly closed it before.
  const saved = localStorage.getItem("ccr.stashOpen");
  if (saved === "0") box.classList.remove("expanded");
  else box.classList.add("expanded");
  header.addEventListener("click", () => {
    const open = box.classList.toggle("expanded");
    localStorage.setItem("ccr.stashOpen", open ? "1" : "0");
  });
})();

// in-app toast：会话状态变到 waiting_permission / needs_input 且不在该会话的 chat 视图时提醒
const _lastNotifiedState = new Map();
function maybeNotify(s) {
  const prev = _lastNotifiedState.get(s.id);
  // ① Toast (页内浮条) — 仅 chat 视图 & 当前未打开此 session 时弹
  const inHomeView = $("view-home").classList.contains("active");
  const isCurrentChat = $("view-chat").classList.contains("active")
                        && state.sessionId === s.id;
  if (!inHomeView && !isCurrentChat) {
    const interesting = s.state === "waiting_permission"
                        || s.state === "needs_input";
    if (interesting && prev !== s.state) {
      showToast(`${s.name} · ${STATE_BADGES[s.state].label}`, s.id);
    }
  }
  // ② Web Notification — 任意 session 的 busy → 非 busy 转换 (turn end).
  // 不看视图, 看 document.visibilityState (在 maybeNotifyTurnEnd 内部判).
  if (prev === "busy" && s.state !== "busy") {
    maybeNotifyTurnEnd(s);
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

// Hub mode 登录后入口: 永远先进 home; 如果用户还没真正完成 server 接入,
// 额外把 onboarding modal 叠在上面 (view-home 在背后还能看见). local mode
// 直接进 home, 不调 modal.
//
// "还没完成接入" = apps 中没有任何一条曾 online 过 (apps.length===0, 或者
// redeem 完但 server 从没连进来 — total_online_seconds===0 且当前 offline).
// 这样: 用户 redeem 后 ✕ 关掉, 刷新还会再弹 (server 一旦上线过就不弹了,
// 哪怕 当时是 offline).
function _needsOnboarding() {
  if (!state.hubMode) return false;
  const apps = state.apps || [];
  if (apps.length === 0) return true;
  return !apps.some(a => a.online || (a.total_online_seconds || 0) > 0);
}

function enterHomeOrOnboarding() {
  enterHome();
  if (_needsOnboarding()) {
    enterOnboarding();
  }
}

function enterHome() {
  showView("home");
  if (!$("spawn-cwd").value) $("spawn-cwd").value = abbreviateHome(state.cwd || "");
  syncPresetChips();
  if (state.hubMode) {
    // hub mode: 必须等 HTTP /api/sessions 填满 sessionsById 后再连 ws-global.
    // 否则 ws delta 先到 (没 app_name/app_id 字段) 渲一次, HTTP 完成又 clear+set
    // 重渲一次 — 用户视觉表现就是 "绿色 badge 一闪而过".
    hubFetchSessions().finally(() => {
      connectGlobalWS();
      // sessions 拿全后判一下要不要弹 step3 "一切就绪" 引导
      // (server 已上线但还没 session → 第一次提示如何创建 session)
      _maybeShowReadyHint();
    });
  } else {
    connectGlobalWS();
  }
  // 拉一次 ~/.claude/settings.json 的 model/effort 默认 (用于 chat-menu
  // Default 选项加注). 只 fetch 一次, 缓存到 state.cliDefaults.
  if (!state.cliDefaults) {
    api("/api/cli/defaults")
      .then(d => { state.cliDefaults = d || {}; })
      .catch(() => { state.cliDefaults = {}; });
  }
}

let _globalBackoff = 1000;
let _globalTimer = null;
function connectGlobalWS() {
  if (state.globalWS
      && (state.globalWS.readyState === WebSocket.OPEN
          || state.globalWS.readyState === WebSocket.CONNECTING)) return;
  if (!state.hubMode && !state.token) return;   // local 未登录
  if (state.hubMode && !state.userId) return;    // hub 未登录
  if (_globalTimer) { clearTimeout(_globalTimer); _globalTimer = null; }
  // hub mode 走 cookie, token 留空; local mode 走 token query.
  const tokenSeg = state.hubMode
    ? ""
    : ("?token=" + encodeURIComponent(state.token));
  const url = wsURL("ws-global" + tokenSeg);
  const ws = new WebSocket(url);
  state.globalWS = ws;
  ws.addEventListener("open", () => { _globalBackoff = 1000; });
  ws.addEventListener("message", (ev) => {
    try { handleGlobalMsg(JSON.parse(ev.data)); }
    catch (e) { console.warn("bad global ws msg", e); }
  });
  ws.addEventListener("close", () => {
    if (state.globalWS === ws) state.globalWS = null;
    if (!state.hubMode && !state.token) return;
    if (state.hubMode && !state.userId) return;
    _globalTimer = setTimeout(connectGlobalWS, _globalBackoff);
    _globalBackoff = Math.min(30000, _globalBackoff * 2);
  });
}

// M-Hub-4: hub mode 用 HTTP 拉 /api/sessions 替代 ws snapshot — server 端
// 返聚合 list (跨所有 user 的 apps). ws-global 还是连, 用于实时 delta.
async function hubFetchSessions() {
  try {
    const list = await api("/api/sessions");
    if (!Array.isArray(list)) return;
    state.sessionsById.clear();
    for (const s of list) {
      state.sessionsById.set(s.id, _withSortKey(s, null));
    }
    renderSessionList();
  } catch (e) {
    console.warn("hubFetchSessions failed:", e.message);
  }
}

// Sessions currently in an optimistic-rename window. Skipping
// renderSessionList for these IDs avoids destroying their cards and
// recreating them just to show the same new name we already painted.
const renameInFlight = new Set();

// §2 active-sort hysteresis: when last_activity_at jumps by ≤ 180 s on a
// SINGLE update (compared to the previously-received last_activity_at),
// the sort snapshot stays put — small consecutive bumps cannot drift the
// snapshot forward cumulatively either. The snapshot only rolls forward
// when a single bump exceeds the threshold.
const ACTIVE_SORT_HYSTERESIS_S = 180;

// 乐观更新 — stash / deactivate / activate 等用. patch 直接合并到 sessionsById
// 然后立即 renderSessionList; 200ms 后 hubFetchSessions refetch 校准.
function _optimisticSessionUpdate(sid, patch) {
  const cur = state.sessionsById.get(sid);
  if (!cur) return;
  state.sessionsById.set(sid, Object.assign({}, cur, patch));
  renderSessionList();
}

function _withSortKey(msg, existing) {
  const newLA = msg.last_activity_at || 0;
  let snap;
  if (!existing) {
    snap = newLA;
  } else {
    const prevLA = (typeof existing._prevLA === "number")
      ? existing._prevLA
      : (existing.last_activity_at || 0);
    const prevSnap = (typeof existing._sortKey === "number")
      ? existing._sortKey
      : prevLA;
    // SINGLE-delta hysteresis: compare new LA to the previously RECEIVED
    // value (which always advances), not to the snapshot. This keeps the
    // snapshot truly sticky across many small bumps.
    snap = Math.abs(newLA - prevLA) > ACTIVE_SORT_HYSTERESIS_S ? newLA : prevSnap;
  }
  return { ...msg, _sortKey: snap, _prevLA: newLA };
}

function handleGlobalMsg(msg) {
  if (msg.type === "snapshot") {
    // Hub mode: HTTP /api/sessions 聚合 list 才是权威 (多 app + 带 app_id 等
    // 字段). ws-global snapshot 只来自单一 app, 字段不全, 会闪 — 整段忽略,
    // 只吃 session_state / session_deleted delta.
    if (state.hubMode) return;
    state.sessionsById.clear();
    for (const s of msg.sessions || []) {
      state.sessionsById.set(s.id, _withSortKey(s, null));
    }
    renderSessionList();
  } else if (msg.type === "session_state") {
    const existing = state.sessionsById.get(msg.id);
    // Hub mode merge — 保留 app_id/app_name/app_online (msg 不带)
    const next = state.hubMode && existing
      ? { ...existing, ...msg }
      : msg;
    state.sessionsById.set(msg.id, _withSortKey(next, existing));
    // Skip the full-list re-render if we just optimistically renamed
    // this session — the card already shows the new name; rebuilding
    // would just flash an empty cell during innerHTML="" reset.
    if (!renameInFlight.has(msg.id)) renderSessionList();
    maybeNotify(msg);
    if (msg.id === state.sessionId) syncChatStatusFromSession(msg);
  } else if (msg.type === "session_deleted") {
    state.sessionsById.delete(msg.id);
    state.sessionCache.delete(msg.id);   // DOM 缓存也清掉，session 没了
    idbDeleteSession(msg.id);            // IDB 缓存也清
    renderSessionList();
  }
  updateTitleBadge();
}

// 让 chat 头部 status 跟 home 列表里的 STATE_BADGES 用同一套语言
function syncChatStatusFromSession(s) {
  if (!s || !s.state) return;
  const badge = STATE_BADGES[s.state] || STATE_BADGES.idle;
  let cls = "";
  if (s.state === "busy" || s.state === "running") cls = "busy";
  else if (s.state === "waiting_permission" || s.state === "needs_input") cls = "error";
  setStatus(cls, badge.label);
}

function updateTitleBadge() {
  let pending = 0;
  for (const s of state.sessionsById.values()) {
    pending += (s.pending_permissions || 0);
  }
  document.title = (pending > 0 ? `[${pending}] ` : "") + "ClaudeCodeRemote";
}

const VALID_SPAWN_PERM_MODES = ["manual", "accept_edits", "plan", "allow_all"];

function _getSpawnPermMode() {
  const active = document.querySelector("#spawn-perm .spawn-perm-btn.active");
  const m = active && active.dataset.mode;
  return VALID_SPAWN_PERM_MODES.includes(m) ? m : "manual";
}

function _setSpawnPermMode(mode) {
  if (!VALID_SPAWN_PERM_MODES.includes(mode)) mode = "manual";
  document.querySelectorAll("#spawn-perm .spawn-perm-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
}

document.querySelectorAll("#spawn-perm .spawn-perm-btn").forEach((b) => {
  b.addEventListener("click", () => _setSpawnPermMode(b.dataset.mode));
});

// Model 选择保持上次值. localStorage 持久化便于跨刷新. Effort 已从 UI 移除.
const VALID_MODEL = ["", "opus", "sonnet", "haiku"];

function _restoreSpawnModel() {
  const m = localStorage.getItem("ccr.spawnModel") || "";
  const ms = $("spawn-model");
  if (ms && VALID_MODEL.includes(m)) ms.value = m;
}
_restoreSpawnModel();

$("spawn-go").addEventListener("click", async () => {
  const name = $("spawn-name").value.trim();
  const cwd = $("spawn-cwd").value.trim();
  const permission_mode = _getSpawnPermMode();
  const model = $("spawn-model") ? $("spawn-model").value : "";
  $("spawn-err").classList.remove("show");
  if (!cwd) {
    $("spawn-err").textContent = "Working directory required";
    $("spawn-err").classList.add("show");
    return;
  }
  $("spawn-go").disabled = true;
  $("spawn-go").textContent = "Starting…";
  try {
    const body = { cwd, name, permission_mode, model, effort: "" };
    // Hub mode: 选定的 app_id 加进 body, 让 hub 决定 forward 目标.
    if (state.hubMode) {
      const sel = $("spawn-app");
      const appId = sel && sel.value;
      if (appId) body.app_id = appId;
    }
    const r = await api("/api/spawn", {
      method: "POST",
      body: JSON.stringify(body),
    });
    state.cwd = cwd;
    // 每个 server 各存自己 last cwd (per-app key); 老的全局 ccr.cwd 不再写,
    // 但保留 read 兼容 (没存过 per-app 时 fallback 到 "~").
    if (state.hubMode) {
      const sel = $("spawn-app");
      const appId = sel ? sel.value : "";
      if (window.__saveCwdForApp) window.__saveCwdForApp(appId, cwd);
    } else {
      localStorage.setItem("ccr.cwd", cwd);
    }
    localStorage.setItem("ccr.spawnModel", model);
    pushRecentCwd(cwd);
    $("spawn-name").value = "";
    if (window.__closeNewModal) window.__closeNewModal();
    enterChat(r.id, r.name, r.cwd);
  } catch (e) {
    $("spawn-err").textContent = "Start failed: " + (e.message || e);
    $("spawn-err").classList.add("show");
  } finally {
    $("spawn-go").disabled = false;
    $("spawn-go").textContent = "Start";
  }
});

// ---------- Quick new (无感新建) ----------
// 点 #new-btn-quick: 立即生成 tmp 前端 session, 进空白 chat, 不起 claude
// CLI. 用户发首条消息时再真 spawn + rename; 不发就离开 → tmp 消失.
$("new-btn-quick").addEventListener("click", () => {
  const tmpId = "tmp-" + Math.random().toString(36).slice(2, 14);
  const now = Date.now() / 1000;
  state.sessionsById.set(tmpId, {
    id: tmpId,
    name: "",
    cwd: "~",
    state: "idle",
    created_at: now,
    last_activity_at: now,
    pending_permissions: 0,
    needs_action_detail: null,
    model: "",
    effort: "",
    cur_model: "",
    _pending: true,
  });
  renderSessionList();
  enterChat(tmpId, "", "~");
});

// 离开 tmp chat (back / 切别的 session / 进 home) 时清理.
// 调用点: chat-back click, enterHome, enterChat 起手 (切到别的 sid).
function _cleanupTmpSessionIfLeaving(nextSid) {
  const curSid = state.sessionId;
  if (!curSid || !curSid.startsWith("tmp-")) return;
  if (nextSid === curSid) return;   // 没切走
  const sess = state.sessionsById.get(curSid);
  if (sess && sess._pending) {
    state.sessionsById.delete(curSid);
    state.sessionCache.delete(curSid);
    renderSessionList();
  }
}

// ---------- New session modal ----------
(function setupNewModal() {
  const btn       = $("new-btn");
  const modal     = $("modal-new-session");
  const closeX    = $("new-modal-close");
  const cancelBtn = $("new-modal-cancel");

  // 每个 server 各自维护 last working directory: ccr.cwd.<app_id>.
  // 切 server 时自动改 spawn-cwd input. 没存过的 app 默认 "~".
  function _cwdKeyForApp(appId) {
    return appId ? `ccr.cwd.${appId}` : "ccr.cwd";
  }
  function _loadCwdForApp(appId) {
    return localStorage.getItem(_cwdKeyForApp(appId)) || "~";
  }
  function _saveCwdForApp(appId, cwd) {
    if (cwd) localStorage.setItem(_cwdKeyForApp(appId), cwd);
  }

  function _fillAppSelect() {
    if (!state.hubMode) return;
    const sel = $("spawn-app");
    if (!sel) return;
    sel.innerHTML = "";
    const apps = (state.apps || []).filter(a => a.online);
    for (const a of apps) {
      const opt = document.createElement("option");
      opt.value = a.id;
      opt.textContent = a.name + (a.online ? "" : " (offline)");
      sel.appendChild(opt);
    }
    // 没有 online app 时, 显示一个 disabled placeholder
    if (!apps.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(no server online)";
      opt.disabled = true;
      sel.appendChild(opt);
    }
    // 切 server 时自动更新 cwd input 到该 server 的 last cwd
    sel.onchange = () => {
      $("spawn-cwd").value = _loadCwdForApp(sel.value);
      syncPresetChips();
    };
  }

  function open() {
    modal.removeAttribute("hidden");
    _fillAppSelect();   // 先填 app select, 才能拿 selected app_id
    // 每次打开 modal 都按当前选中 server 重设 cwd input — 不再用全局 state.cwd
    const sel = $("spawn-app");
    const appId = state.hubMode && sel ? sel.value : "";
    $("spawn-cwd").value = _loadCwdForApp(appId);
    syncPresetChips();
    _setSpawnPermMode(
      localStorage.getItem("ccr.defaultPermMode") || "manual"
    );
    setTimeout(() => $("spawn-name").focus(), 0);
  }
  // 把 save helper 暴露给外面 spawn-go click handler 用 (跨 IIFE)
  window.__saveCwdForApp = _saveCwdForApp;
  function close() {
    modal.setAttribute("hidden", "");
    $("spawn-err").classList.remove("show");
  }
  // Make it reachable from spawn-go success path so we can hide the modal
  // once we're on our way to chat view.
  window.__closeNewModal = close;

  btn.addEventListener("click", () => {
    // 如果 onboarding step3 coachmark 正显示, 顺手 dismiss — 用户已经
    // 跟着箭头点了真实 + 按钮, coachmark 任务完成
    if (typeof _onboardActive !== "undefined" && _onboardActive) {
      try { exitOnboarding(); } catch (_) {}
    }
    open();
  });
  closeX.addEventListener("click", close);
  cancelBtn.addEventListener("click", close);
  modal.addEventListener("click", (e) => {
    if (e.target.id === "modal-new-session") close();   // backdrop only
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hasAttribute("hidden")) close();
  });
})();

// ---------- Settings view ----------
(function setupSettings() {
  const view = $("view-settings");
  const openBtn = $("settings-btn");
  const backBtn = $("settings-back");
  const permRow = $("settings-default-perm");
  if (!view || !openBtn) return;

  function applyPermActive(mode) {
    if (!VALID_SPAWN_PERM_MODES.includes(mode)) mode = "manual";
    permRow.querySelectorAll(".spawn-perm-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === mode);
    });
  }
  function loadFromStorage() {
    applyPermActive(localStorage.getItem("ccr.defaultPermMode") || "manual");
  }
  function isOpen() { return view.classList.contains("active"); }
  function open() {
    if (isOpen()) return;
    loadFromStorage();
    view.classList.add("active");
  }
  function close() {
    view.classList.remove("active");
  }

  openBtn.addEventListener("click", open);
  backBtn.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen()) close();
  });

  permRow.querySelectorAll(".spawn-perm-btn").forEach((b) => {
    b.addEventListener("click", () => {
      const mode = b.dataset.mode;
      applyPermActive(mode);
      localStorage.setItem("ccr.defaultPermMode", mode);
    });
  });
})();

// ---------- Session list sort toggle ----------
(function setupSortToggle() {
  const btn = $("sessions-sort");
  if (!btn) return;
  setSortMode(getSortMode());   // sync UI to stored mode
  btn.addEventListener("click", () => {
    const next = getSortMode() === "created" ? "active" : "created";
    setSortMode(next);
    renderSessionList();
  });
})();

// ---------- Help view (无需登录可见) ----------
(function setupHelp() {
  const view = $("view-help");
  const back = $("help-back");
  const helpLink = $("help-link");
  const loginHelpLink = $("login-help-link");
  const urlEl = $("help-url");
  const urlCopyBtn = $("help-url-copy");
  const envEl = $("help-env-snippet");
  const envCopyBtn = $("help-env-copy");
  const dockerEnvEl = $("help-docker-env");
  const dockerEnvCopyBtn = $("help-docker-env-copy");
  if (!view) return;

  function fill() {
    const baseUrl = location.origin + (location.pathname.replace(/\/$/, "") || "");
    // banner 让 AI agent 直接读到这页指南, URL 带 #help hash.
    urlEl.textContent = baseUrl + "/#help";
    const wsUrl = baseUrl.replace(/^http/, "ws");
    envEl.textContent =
      `CCR_TOKEN=$(openssl rand -hex 16)
CCR_HUB_URL=${wsUrl}
CCR_HUB_DEVICE_TOKEN=tok-paste-from-cloud-servers`;
    dockerEnvEl.textContent =
      `CCR_TOKEN=$(openssl rand -hex 16)
CCR_HUB_URL=${wsUrl}
CCR_HUB_DEVICE_TOKEN=tok-paste-from-cloud-servers
ANTHROPIC_API_KEY=                    # optional, blank = mock`;
  }
  function open() {
    if (view.classList.contains("active")) return;
    fill();
    view.classList.add("active");
  }
  function close() {
    view.classList.remove("active");
    // 清掉 #help hash 否则下次再打开页面就自动进 help
    if (location.hash === "#help") {
      history.replaceState(null, "", location.pathname + location.search);
    }
  }
  if (helpLink) helpLink.addEventListener("click", e => { e.preventDefault(); open(); });
  if (loginHelpLink) loginHelpLink.addEventListener("click", e => { e.preventDefault(); open(); });
  back.addEventListener("click", close);
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && view.classList.contains("active")) close();
  });
  // 启动时检测 #help hash, 自动开 (用户从分享链接进来)
  if (location.hash === "#help") {
    setTimeout(open, 0);
  }
  window.addEventListener("hashchange", () => {
    if (location.hash === "#help") open();
  });

  async function _copy(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      // icon-only 按钮 (含 SVG 子元素) 走 class flash, 不动 innerHTML.
      // 普通 text 按钮直接改 textContent 显 "Copied!".
      if (btn.querySelector("svg")) {
        btn.classList.add("copied");
        setTimeout(() => btn.classList.remove("copied"), 1200);
      } else {
        const old = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = old; }, 1200);
      }
    } catch (e) {
      alert("Copy failed. Select the text and Ctrl-C.");
    }
  }
  urlCopyBtn.addEventListener("click", () => _copy(urlEl.textContent, urlCopyBtn));
  envCopyBtn.addEventListener("click", () => _copy(envEl.textContent, envCopyBtn));
  dockerEnvCopyBtn.addEventListener("click",
    () => _copy(dockerEnvEl.textContent, dockerEnvCopyBtn));
})();

// ---------- Apps (Cloud server) management view ----------
(function setupApps() {
  const view = $("view-apps");
  const openBtn = $("apps-btn");
  const backBtn = $("apps-back");
  const newBtn = $("apps-new-btn");
  const listEl = $("apps-list");
  const panel = $("new-app-panel");
  const step1 = $("new-app-step1");
  const step2 = $("new-app-step2");
  const nameInput = $("new-app-name");
  const errEl = $("new-app-err");
  const goBtn = $("new-app-go");
  const cancelBtn = $("new-app-cancel");
  const closeBtn = $("new-app-close");
  const doneBtn = $("new-app-done");
  const tokenEl = $("new-app-token");
  const envEl = $("new-app-env");
  const tokenCopyBtn = $("new-app-token-copy");
  const envCopyBtn = $("new-app-env-copy");
  if (!view || !openBtn) return;

  function isOpen() { return view.classList.contains("active"); }
  async function reload() {
    try {
      const apps = await api("/api/hub/apps");
      renderList(apps);
    } catch (e) {
      listEl.innerHTML = `<div class="apps-empty">Failed to load: ${escHTML(e.message || String(e))}</div>`;
    }
  }
  function _fmtDuration(secs) {
    secs = Math.max(0, Math.floor(secs || 0));
    if (secs < 60)      return `${secs}s`;
    if (secs < 3600)    return `${Math.floor(secs / 60)}m`;
    if (secs < 86400)   return `${Math.floor(secs / 3600)}h`;
    return `${Math.floor(secs / 86400)}d`;
  }
  function renderList(apps) {
    if (!apps || !apps.length) {
      listEl.innerHTML = '<div class="apps-empty">No servers registered yet. Click + to add one.</div>';
      return;
    }
    listEl.innerHTML = "";
    const now = Date.now() / 1000;
    for (const a of apps) {
      const row = document.createElement("div");
      row.className = "app-row " + (a.online ? "online" : "offline");
      row.dataset.appId = a.id;
      const authoredAgo = a.created_at ? _fmtDuration(now - a.created_at) : "?";
      // online: 显示当前 session 持续时长 ("Connected for X"). offline: 不显示.
      let connectedHTML = "";
      if (a.online && a.connected_at) {
        const cur = _fmtDuration(now - a.connected_at);
        connectedHTML = `
          <span class="sep">·</span>
          <span class="app-stat">Connected for ${escHTML(cur)}</span>`;
      }
      row.innerHTML = `
        <span class="state-dot" aria-hidden="true"></span>
        <div class="app-rows">
          <div class="app-row-1">
            <span class="app-name"></span>
          </div>
          <div class="app-row-2">
            <span class="app-stat">Authorized ${escHTML(authoredAgo)} ago</span>
            ${connectedHTML}
          </div>
        </div>
        <button class="app-revoke" type="button" title="Revoke" aria-label="Revoke">✕</button>
      `;
      row.querySelector(".app-name").textContent = a.name || "(unnamed)";
      row.querySelector(".app-revoke").addEventListener("click", async () => {
        if (!confirm(`Revoke "${a.name}"? Its device token will be invalidated.`)) return;
        try {
          await api(`/api/hub/apps/${encodeURIComponent(a.id)}`, { method: "DELETE" });
          reload();
        } catch (e) {
          alert("Revoke failed: " + (e.message || e));
        }
      });
      listEl.appendChild(row);
    }
    _attachDragReorder();
  }

  // iOS-style long-press drag reorder. 长按 500ms 后进 drag 状态,
  // 跟手 translateY, 中点检测自动 swap, 释放时 PUT /api/hub/apps/reorder
  // 持久化 (多设备同步 — 任何登录设备拉 /api/hub/apps 都按新顺序).
  function _attachDragReorder() {
    const LONG_PRESS_MS = 500;
    const MOVE_CANCEL_PX = 8;
    listEl.querySelectorAll(".app-row").forEach((row) => {
      let pressTimer = null;
      let dragState = null;
      let startX = 0, startY = 0;
      let pid = null;
      const onDown = (e) => {
        if (e.target.closest(".app-revoke")) return;
        if (e.button !== undefined && e.button !== 0) return;
        startX = e.clientX; startY = e.clientY; pid = e.pointerId;
        pressTimer = setTimeout(() => { pressTimer = null; _startDrag(e); }, LONG_PRESS_MS);
      };
      const onMove = (e) => {
        if (pressTimer) {
          if (Math.abs(e.clientX - startX) > MOVE_CANCEL_PX
              || Math.abs(e.clientY - startY) > MOVE_CANCEL_PX) {
            clearTimeout(pressTimer); pressTimer = null;
          }
          return;
        }
        if (dragState) _moveDrag(e);
      };
      const onUp = (e) => {
        if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
        if (dragState) _endDrag(e);
      };
      function _startDrag(e) {
        const all = Array.from(listEl.querySelectorAll(".app-row"));
        const myIdx = all.indexOf(row);
        if (myIdx < 0) return;
        dragState = {
          myIdx, currentIdx: myIdx, startY: e.clientY,
          items: all.map((r) => {
            const rc = r.getBoundingClientRect();
            return { row: r, h: rc.height };
          }),
        };
        row.classList.add("dragging");
        document.body.classList.add("apps-reordering");
        try { row.setPointerCapture(pid); } catch (_) {}
        if (navigator.vibrate) navigator.vibrate(15);
      }
      function _moveDrag(e) {
        const dy = e.clientY - dragState.startY;
        row.style.transform = `translateY(${dy}px) scale(1.03)`;
        const myH = dragState.items[dragState.myIdx].h;
        // 算新 idx: dy / myH 四舍五入然后 clamp
        const shift = Math.round(dy / myH);
        let newIdx = dragState.myIdx + shift;
        newIdx = Math.max(0, Math.min(dragState.items.length - 1, newIdx));
        if (newIdx !== dragState.currentIdx) {
          dragState.items.forEach((itm, i) => {
            if (i === dragState.myIdx) return;
            let t = 0;
            if (dragState.myIdx < newIdx && i > dragState.myIdx && i <= newIdx) t = -myH;
            else if (dragState.myIdx > newIdx && i >= newIdx && i < dragState.myIdx) t = myH;
            itm.row.style.transform = t ? `translateY(${t}px)` : "";
          });
          dragState.currentIdx = newIdx;
        }
      }
      function _endDrag(_e) {
        // 算 final order
        const items = dragState.items;
        const moved = items.splice(dragState.myIdx, 1)[0];
        items.splice(dragState.currentIdx, 0, moved);
        const orderedIds = items.map((itm) => itm.row.dataset.appId);
        const noChange = dragState.myIdx === dragState.currentIdx;
        // 关键: 立即在 DOM 层 reorder, 不等 server 回. 跟 inline transform
        // 清空同步发生, 让 row 从"跟手 translated 位置"平滑 snap 到"新 DOM 位置".
        // 不再等 reload — 视觉 100% 由本地 state 驱动, server 失败才 reload 校准.
        items.forEach((itm) => listEl.appendChild(itm.row));
        // 让 the dragged row 平滑 settle 到新位置 (它现在的 transform 是 dy,
        // 新 DOM idx 是 currentIdx, 清 transform 后差距由 200ms transition 化解).
        row.style.transition = "transform 200ms cubic-bezier(0.25, 1, 0.5, 1)";
        row.style.transform = "";
        items.forEach((itm) => {
          if (itm.row !== row) itm.row.style.transform = "";
        });
        setTimeout(() => {
          row.style.transition = "";
          row.classList.remove("dragging");
          document.body.classList.remove("apps-reordering");
        }, 220);
        dragState = null;
        if (noChange) return;
        // background 持久化, 失败 reload 校准 (用户视觉已经是 final state)
        api("/api/hub/apps/reorder", {
          method: "PUT",
          body: JSON.stringify({ ordered_ids: orderedIds }),
        }).catch((e) => {
          alert("reorder failed: " + (e.message || e));
          reload();
        });
      }
      row.addEventListener("pointerdown", onDown);
      row.addEventListener("pointermove", onMove);
      row.addEventListener("pointerup", onUp);
      row.addEventListener("pointercancel", onUp);
    });
  }
  function open() {
    if (isOpen()) return;
    view.classList.add("active");
    reload();
  }
  function close() {
    view.classList.remove("active");
  }
  openBtn.addEventListener("click", open);
  backBtn.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen()) {
      if (!panel.hidden) hidePanel();
      else close();
    }
  });

  // ---- New app inline panel (内嵌, 不是 modal) ----
  function showPanel() {
    step1.hidden = false;
    step2.hidden = true;
    nameInput.value = "";
    errEl.classList.remove("show");
    errEl.textContent = "";
    panel.removeAttribute("hidden");
    setTimeout(() => nameInput.focus(), 0);
  }
  function hidePanel() { panel.setAttribute("hidden", ""); }

  newBtn.addEventListener("click", showPanel);
  closeBtn.addEventListener("click", hidePanel);
  cancelBtn.addEventListener("click", hidePanel);
  doneBtn.addEventListener("click", () => { hidePanel(); reload(); });

  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") goBtn.click();
  });

  goBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    errEl.classList.remove("show");
    if (!name) {
      errEl.textContent = "Name required";
      errEl.classList.add("show");
      return;
    }
    goBtn.disabled = true;
    goBtn.textContent = "Creating…";
    try {
      // 1. POST /api/hub/pair → 拿 code (登录用户调)
      const pair = await api("/api/hub/pair", { method: "POST" });
      // 2. 立刻 redeem (无需 cookie 但带也行) → 拿 device_token
      const red = await api("/api/hub/pair/redeem", {
        method: "POST",
        body: JSON.stringify({ code: pair.code, app_name: name }),
      });
      // 3. 显示给用户复制
      tokenEl.textContent = red.device_token;
      envEl.textContent =
        `CCR_HUB_URL=wss://${location.host}\n` +
        `CCR_HUB_DEVICE_TOKEN=${red.device_token}\n` +
        `CCR_HUB_APP_NAME=${red.app_name}`;
      step1.hidden = true;
      step2.hidden = false;
    } catch (e) {
      errEl.textContent = "Create failed: " + (e.message || e);
      errEl.classList.add("show");
    } finally {
      goBtn.disabled = false;
      goBtn.textContent = "Create";
    }
  });

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      const old = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(() => { btn.textContent = old; }, 1200);
    } catch (e) {
      alert("Copy failed; please select + Ctrl-C manually.");
    }
  }
  tokenCopyBtn.addEventListener("click", () => copyText(tokenEl.textContent, tokenCopyBtn));
  envCopyBtn.addEventListener("click", () => copyText(envEl.textContent, envCopyBtn));
})();

// ---------- 新用户首次接入 (#modal-onboarding) ----------
// hub mode + state.apps 空时, modal 叠在 view-home 之上 (home 在背后正常).
// Step1 输入 server 名 → POST /api/hub/pair → /redeem 拿 device token →
// Step2 展示"扔给 Claude Code"的完整 prompt + 一键复制. 后台每 4s GET
// /api/me, 看到 apps.some(online) 自动关闭弹窗 (home 已经在背后, 不切换).
let _onboardPollTimer = null;
let _onboardActive = false;
// Page-session 范围的 flag: step3 一旦展示过 (任何路径), 这次不再 auto-pop.
// 用户刷新页面 (新 page session) 重置 — 如果还是没 session, 还会再弹一次.
let _readyHintShownThisSession = false;

// Step3 单独入口: hub mode + 已有 online server + 无 session 时, enterHome
// 后弹这个 "现在点 ➕ 新建 session" 引导. coachmark 风格 — backdrop 几乎
// 透明, 浮层箭头指真实 #new-btn, 用户点按钮直接开 new-session modal.
function enterOnboardingStep3() {
  _onboardActive = true;
  _readyHintShownThisSession = true;
  const modal = $("modal-onboarding");
  if (!modal) return;
  const step1 = $("onboard-step1");
  const step2 = $("onboard-step2");
  const step3 = $("onboard-step3");
  if (step1) step1.hidden = true;
  if (step2) step2.hidden = true;
  if (step3) step3.hidden = false;
  modal.classList.add("step3-mode");
  modal.removeAttribute("hidden");
  // 下一帧再算 coach-arrow 位置 — modal 刚显示 layout 可能还没稳
  requestAnimationFrame(_positionCoachArrow);
  window.addEventListener("resize", _positionCoachArrow);
}

function _positionCoachArrow() {
  if (!_onboardActive) return;
  const arrow = $("onboard-coach-arrow");
  const btn = $("new-btn");
  if (!arrow || !btn) return;
  const rect = btn.getBoundingClientRect();
  // 箭头放按钮正下方, 水平居中对齐按钮
  arrow.hidden = false;
  // 算好宽度再 center
  const w = arrow.getBoundingClientRect().width || 80;
  arrow.style.left = (rect.left + rect.width / 2 - w / 2) + "px";
  arrow.style.top = (rect.bottom + 8) + "px";
  btn.classList.add("new-btn-coach-pulse");
}

function _hideCoachArrow() {
  $("onboard-coach-arrow")?.setAttribute("hidden", "");
  $("new-btn")?.classList.remove("new-btn-coach-pulse");
  window.removeEventListener("resize", _positionCoachArrow);
}

function _maybeShowReadyHint() {
  if (_onboardActive) return;
  if (_readyHintShownThisSession) return;
  if (!state.hubMode) return;
  const apps = state.apps || [];
  const hasOnlineApp = apps.some(a => a.online);
  const noSessions = state.sessionsById.size === 0;
  if (hasOnlineApp && noSessions) enterOnboardingStep3();
}

function enterOnboarding() {
  _onboardActive = true;
  const modal = $("modal-onboarding");
  if (!modal) return;
  const step1 = $("onboard-step1");
  const step2 = $("onboard-step2");
  const step3 = $("onboard-step3");
  if (step1) step1.hidden = false;
  if (step2) step2.hidden = true;
  if (step3) step3.hidden = true;
  modal.removeAttribute("hidden");
  _readyHintShownThisSession = true;   // 这次 page session 不再 auto-pop step3
  const nameEl = $("onboard-name");
  if (nameEl) {
    if (!nameEl.value) nameEl.value = "MyFirstServer";
    setTimeout(() => { try { nameEl.focus(); nameEl.select(); } catch (_) {} }, 50);
  }
  _stopOnboardPoll();
}

function exitOnboarding() {
  _onboardActive = false;
  _stopOnboardPoll();
  _hideCoachArrow();
  const modal = $("modal-onboarding");
  if (modal) {
    modal.setAttribute("hidden", "");
    modal.classList.remove("step3-mode");
  }
}

function _stopOnboardPoll() {
  if (_onboardPollTimer) {
    clearInterval(_onboardPollTimer);
    _onboardPollTimer = null;
  }
}

function _startOnboardPoll() {
  _stopOnboardPoll();
  _onboardPollTimer = setInterval(async () => {
    if (!_onboardActive) { _stopOnboardPoll(); return; }
    try {
      const me = await api("/api/me");
      const apps = me.apps || [];
      // 关键: "至少一个 server 已 online", 不是 "apps 非空".
      // redeem 完了 apps 立即就有一条 (offline), 那时用户还在读 prompt.
      const anyOnline = apps.some(a => a.online);
      if (anyOnline) {
        state.apps = apps;
        _stopOnboardPoll();
        // 不直接关弹窗 — 切到 step3 "一切就绪" 成功态, 引导用户用 + 按钮.
        // 切的同时 modal 进 coachmark 模式 (淡化背景, 浮层箭头指 #new-btn).
        const modal = $("modal-onboarding");
        const step2 = $("onboard-step2");
        const step3 = $("onboard-step3");
        if (step2) step2.hidden = true;
        if (step3) step3.hidden = false;
        if (modal) modal.classList.add("step3-mode");
        _readyHintShownThisSession = true;
        requestAnimationFrame(_positionCoachArrow);
        window.addEventListener("resize", _positionCoachArrow);
        hubFetchSessions();
      }
    } catch (_) {
      // 静默 — 网络抖动不应中断引导
    }
  }, 4000);
}

(function setupOnboarding() {
  const modal = $("modal-onboarding");
  if (!modal) return;
  const step1 = $("onboard-step1");
  const step2 = $("onboard-step2");
  const nameEl = $("onboard-name");
  const errEl = $("onboard-err");
  const goBtn = $("onboard-go");
  const closeBtn = $("onboard-close");
  const promptEl = $("onboard-prompt");
  const promptCopyBtn = $("onboard-prompt-copy");
  const tokenPreviewEl = $("onboard-token-preview");
  const backLink = $("onboard-back-link");

  function _buildPrompt(host, token, appName) {
    const wsUrl = "wss://" + host;
    return (
`请帮我在这台机器上装好 ClaudeCodeRemote (CCR) 的 server 端, 让它接入
我现有的 hub.

背景: CCR 是自建的 Claude Code 远程控制台. 架构是
  PWA  ↔  Hub (FastAPI 中心, 鉴权 + 聚合)  ↔  反向 WS tunnel
       ↔  CCR server (× N, 每台机器跑一份)  ↔  claude CLI 子进程
每台机器跑一份 CCR server 进程, 主动连出 Hub (不需要在这台机器上开
公网入站口); server 本地用 stream-json 协议起 claude CLI 子进程,
Hub 把 PWA 的请求 (聊天 / 工具批准 / diff / 通知) 转过来, 我就能在
手机或任何浏览器上跟 Claude 聊这台机器上的项目.

你现在要做的: 把 CCR server 装到这台机器上, 用下面的 device token
鉴权接入我的 Hub, 然后长期跑 (systemd user service 守护). 我在 PWA
上看到这台机器上线就成功了.

env 接入参数 (写到 EnvironmentFile, 比如 ~/.config/ccr/env):

  CCR_TOKEN=$(openssl rand -hex 16)
  CCR_HUB_URL=${wsUrl}
  CCR_HUB_DEVICE_TOKEN=${token}
  CCR_HUB_APP_NAME=${appName}

安装 + 启动:
- pip install claude-code-remote (建议独立 venv, 比如 ~/.venv/ccr)
- systemd user service, ExecStart 走:
    python -m uvicorn claude_code_remote.server.main:app
- systemctl --user enable --now ccr.service
- journalctl --user -u ccr -f 看到 "hub_client connected" 即成功.
  (顺手 sudo loginctl enable-linger $USER, 注销后也跑)

项目完整信息 (架构图 / REQUIREMENTS / 部署 example / 源码):
  https://github.com/hwaipy/ClaudeCodeRemote
PyPI:
  https://pypi.org/project/claude-code-remote/
不确定的细节优先去 README + deploy/ccr.service.example 对照, 别瞎猜.

完成后告诉我 "已上线".`);
  }

  nameEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") goBtn.click();
  });

  goBtn?.addEventListener("click", async () => {
    if (errEl) errEl.classList.remove("show");
    let name = (nameEl.value || "").trim();
    if (!name) name = "MyFirstServer";
    goBtn.disabled = true;
    const oldTxt = goBtn.textContent;
    goBtn.textContent = "生成中…";
    try {
      const pair = await api("/api/hub/pair", { method: "POST" });
      const red = await api("/api/hub/pair/redeem", {
        method: "POST",
        body: JSON.stringify({ code: pair.code, app_name: name }),
      });
      const token = red.device_token;
      const appName = red.app_name || name;
      const host = location.host;
      const promptText = _buildPrompt(host, token, appName);
      if (promptEl) promptEl.textContent = promptText;
      if (tokenPreviewEl) tokenPreviewEl.textContent = token;
      step1.hidden = true;
      step2.hidden = false;
      _startOnboardPoll();
    } catch (e) {
      if (errEl) {
        errEl.textContent = "生成失败: " + (e.message || e);
        errEl.classList.add("show");
      }
    } finally {
      goBtn.disabled = false;
      goBtn.textContent = oldTxt;
    }
  });

  promptCopyBtn?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(promptEl.textContent || "");
      const old = promptCopyBtn.textContent;
      promptCopyBtn.textContent = "已复制";
      setTimeout(() => { promptCopyBtn.textContent = old; }, 1200);
    } catch (e) {
      alert("复制失败, 请手动选中文本 Ctrl-C");
    }
  });

  backLink?.addEventListener("click", (e) => {
    e.preventDefault();
    _stopOnboardPoll();
    step2.hidden = true;
    step1.hidden = false;
    setTimeout(() => { try { nameEl.focus(); nameEl.select(); } catch (_) {} }, 50);
  });

  closeBtn?.addEventListener("click", () => { exitOnboarding(); });
  $("onboard-done")?.addEventListener("click", () => { exitOnboarding(); });

  // 点 modal 外的背景区域 (modal-bg) 关闭. 但点 modal 内部不关.
  modal.addEventListener("click", (e) => {
    if (e.target === modal) exitOnboarding();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _onboardActive) exitOnboarding();
  });

  // help 视图 z-index (35) 低于 modal (200), 用户从 onboarding 点 #help
  // 链接, help slide-in 会被 modal 挡住 — 直接关 modal, 让 help 接管.
  const helpLink = $("onboard-help-link");
  helpLink?.addEventListener("click", () => { exitOnboarding(); });
})();

// ---------- Session list search ----------
(function setupSearch() {
  const btn    = $("search-btn");
  const input  = $("search-input");
  const clear  = $("search-clear");
  const bar    = $("search-bar");
  const wrap   = document.querySelector(".home-top");
  // Pin natural widths of every .icon-btn (settings + new) so the
  // width transition has two concrete pixel endpoints — auto → 0 won't
  // animate smoothly.
  const iconBtns = wrap ? wrap.querySelectorAll(".icon-btn") : [];

  function applyFilter() {
    const q = (input.value || "").trim().toLowerCase();
    document.querySelectorAll(".session-card").forEach(card => {
      const name = (card.querySelector(".name")?.textContent || "").toLowerCase();
      const match = !q || name.includes(q);
      card.hidden = !match;
    });
  }
  function isOpen() { return wrap && wrap.classList.contains("search-open"); }

  function open() {
    if (!wrap || isOpen()) return;
    iconBtns.forEach(b => {
      b.style.width = b.getBoundingClientRect().width + "px";
    });
    if (iconBtns[0]) void iconBtns[0].offsetWidth;   // single reflow
    wrap.classList.add("search-open");
    // Focus synchronously in the click handler so iOS keeps the user
    // gesture and shows the keyboard. setTimeout would lose that context.
    input.focus();
    input.select();
  }
  function close() {
    input.value = "";
    if (wrap) wrap.classList.remove("search-open");
    applyFilter();
    setTimeout(() => {
      iconBtns.forEach(b => { b.style.width = ""; });
    }, 500);
  }

  btn.addEventListener("click", open);
  clear.addEventListener("click", e => { e.stopPropagation(); close(); });
  input.addEventListener("input", applyFilter);
  input.addEventListener("keydown", e => {
    if (e.key === "Escape") close();
  });

  // Auto-collapse on click outside the bar. Use mousedown so the close
  // animation starts even before the user releases the button. Skip
  // touches that target the bar itself (e.g., tapping the input).
  document.addEventListener("mousedown", e => {
    if (!isOpen()) return;
    if (bar.contains(e.target)) return;
    close();
  });
  document.addEventListener("touchstart", e => {
    if (!isOpen()) return;
    if (bar.contains(e.target)) return;
    close();
  }, { passive: true });
})();

// ---------- Chat ----------
function saveCurrentSessionCache() {
  if (!state.sessionId) return;
  // tmp _pending session 不缓存 — 它跟着会被清掉, 没必要存
  if (state.sessionId.startsWith("tmp-")) return;
  const log = $("chat-log");
  // 抓 scrollTop 必须在 innerHTML 抽走之前 — 把子节点挪到 fragment 后 scrollTop 会重置
  const scrollTop = log.scrollTop;
  // 把 chat-log 的子节点抽出来，DOM 引用（toolById / msgById 里的 .card / .bubble）依然有效
  const frag = document.createDocumentFragment();
  while (log.firstChild) frag.appendChild(log.firstChild);
  // LRU：已存在的先删再插，命中放在最后；超额从最旧的开始淘汰
  if (state.sessionCache.has(state.sessionId)) state.sessionCache.delete(state.sessionId);
  state.sessionCache.set(state.sessionId, {
    dom: frag,
    scrollTop,
    msgById:          state.msgById,
    toolById:         state.toolById,
    askuserById:      state.askuserById,
    blocksByIdx:      state.blocksByIdx,
    firstSeq:         state.firstSeq,
    hasMoreHistory:   state.hasMoreHistory,
    maxSeq:           state.maxSeq || 0,
    turnStartAt:      state.turnStartAt,
    turnEndAt:        state.turnEndAt,
    curOutputTokens:  state.curOutputTokens,
    priorTurnOutput:  state.priorTurnOutput,
    lastInputTokens:  state.lastInputTokens,
    lastInputTotal:   state.lastInputTotal,
    currentMsgModel:  state.currentMsgModel,
    contextLimit:     state.contextLimit,
    totalCostUsd:     state.totalCostUsd,
    currentToolGroup: state.currentToolGroup,
    cwdShort:         state.cwdShort,
  });
  while (state.sessionCache.size > SESSION_CACHE_MAX) {
    const oldest = state.sessionCache.keys().next().value;
    state.sessionCache.delete(oldest);
  }
}

function restoreSessionCache(cached) {
  const log = $("chat-log");
  log.innerHTML = "";
  log.appendChild(cached.dom);   // fragment 内子节点转移回 chat-log，fragment 自身清空（下次保存再装）
  // 恢复 scrollTop — 必须用 instant (避免 chat-log 的 scroll-behavior: smooth
  // 走动画). 必须在 layout 完成后写, 不然 scrollHeight 还没算好.
  // requestAnimationFrame 保证下一帧 layout 计算完, scrollTop 写入有效.
  const _restoreTop = typeof cached.scrollTop === "number"
    ? cached.scrollTop : log.scrollHeight;
  const _prevBeh = log.style.scrollBehavior;
  log.style.scrollBehavior = "auto";
  log.scrollTop = _restoreTop;
  log.style.scrollBehavior = _prevBeh;
  // 兜底 (cache hit 后 first_paint 不会触发, 但有些异步 layout 后才稳定): 再写一次
  requestAnimationFrame(() => {
    log.style.scrollBehavior = "auto";
    log.scrollTop = _restoreTop;
    log.style.scrollBehavior = _prevBeh;
    // 同步 state.atBottom — 否则恢复到非底部时, 下一条新消息会被
    // chatScrollBottom 拉到底, 破坏 §4 的"不抢阅读位置"契约.
    const dist = log.scrollHeight - log.scrollTop - log.clientHeight;
    state.atBottom = dist < 40;
  });
  state.msgById         = cached.msgById;
  state.toolById        = cached.toolById;
  state.askuserById     = cached.askuserById || new Map();
  state.blocksByIdx     = cached.blocksByIdx;
  state.activeMsgId     = null;    // 离开时若有未完成 message，msgId 失效；下条 message_start 会重置
  state.firstSeq        = cached.firstSeq;
  state.hasMoreHistory  = cached.hasMoreHistory;
  state.maxSeq          = cached.maxSeq || 0;
  state.turnStartAt     = cached.turnStartAt;
  state.turnEndAt       = cached.turnEndAt;
  state.curOutputTokens = cached.curOutputTokens || 0;
  state.priorTurnOutput = cached.priorTurnOutput || 0;
  state.lastInputTokens = cached.lastInputTokens || 0;
  state.lastInputTotal  = cached.lastInputTotal || 0;
  state.currentMsgModel = cached.currentMsgModel || "";
  state.contextLimit    = cached.contextLimit || 200_000;
  state.totalCostUsd    = cached.totalCostUsd || 0;
  state.currentToolGroup = cached.currentToolGroup;
  state.cwdShort        = cached.cwdShort || "";
  state.isHistoryReplay = false;   // 缓存命中视为已在实时模式
  state.earlierFragment = null;
  state.turnFresh = false;          // 重新进入：本次还没看到 live 轮次动作；停止状态不显示 token/time
}

async function enterChat(id, name, cwd, sessionState) {
  // [telemetry] 跟踪冷启动各阶段耗时. state._enterChatT0 是基准
  state._enterChatT0 = performance.now();
  try { dbgLog("enterChat-start", { id, cacheHit: state.sessionCache.has(id) }); } catch (_) {}
  // 进新 session 前, 若离开的是 tmp _pending session 且没真 spawn 过, 清掉
  _cleanupTmpSessionIfLeaving(id);
  // 切 session 前必须断 turn-card observer + 清 reference. 否则旧 session
  // 的 active card 会被 MutationObserver 推到**新 session** 的 chat-log 末尾
  // (chat-log DOM element 复用, innerHTML="" 后 observer 还指着旧 card 试图
  // appendChild → 串 session).
  if (state._turnCardObserver) {
    state._turnCardObserver.disconnect();
    state._turnCardObserver = null;
  }
  state._turnCard = null;
  // 重置 dup 诊断: 上 session 的 dup 报告跟新 session 不相关; banner 也清.
  _dupDiagBuffer.length = 0;
  _hideDupDiagBanner();
  if (_turnCardMutationObserver) {
    _turnCardMutationObserver.disconnect();
    _turnCardMutationObserver = null;
  }
  // 切 session 前先把当前 session 的 DOM/state 缓存起来
  if (state.sessionId && state.sessionId !== id) {
    saveCurrentSessionCache();
  }
  // 切 session 前必须先关旧 ws：否则旧 session 的事件会写到新 session 的 chat-log 上（串 session）
  if (state.ws) {
    try { state.ws.close(); } catch (_) {}
    state.ws = null;
  }
  state.sessionId = id;
  // 切 sid 第一时间 restore chat-input 草稿 — 避免上 session 输入残留. 任何
  // 后续 enterChat 早 return 路径 (tmp / cache hit / cache miss) 都已经覆盖.
  restoreChatInputDraft(id);
  document.body.classList.add("has-session");
  renderSessionList();   // 让列表的 "当前" 高亮标记跟随切换
  state.loadingHistory = false;
  state.suppressScrollLoad = true;
  setTimeout(() => { state.suppressScrollLoad = false; }, 500);
  // 清掉上一次可能残留的 inline transform/transition，避免影响这次滑入动画.
  // chat-head 是 view-chat 的兄弟, swipe-back 时被同步驱动了 inline, 也要清.
  const _chatView = $("view-chat");
  _chatView.style.transform = "";
  _chatView.style.transition = "";
  const _chatHead = document.getElementById("chat-head");
  if (_chatHead) {
    _chatHead.style.transform = "";
    _chatHead.style.transition = "";
  }
  $("chat-name").textContent = name || "untitled";
  // 立即按默认显示 perm 按钮（用 manual），等 GET 回来再修正
  applyPermissionMode("manual");
  // tmp session 不存在于后端 → 不调 loadPermissionMode (会 404). 也不
  // connectWS, 不显示 chat-loading. 直接揭幕空白 chat, 等用户首条 send.
  if (id.startsWith("tmp-")) {
    state.cacheHit = false;
    state.msgById.clear();
    state.toolById.clear();
    state.askuserById.clear();
    state.firstSeq = null;
    state.hasMoreHistory = false;
    state.maxSeq = 0;
    state.cwdShort = "~";
    $("chat-log").innerHTML = "";
    $("chat-loading").hidden = true;
    setConnDot("", "");
    state.revealChat = () => showView("chat");
    state.revealChat();
    refreshChatMeta();
    refreshConvStatus();
    setTimeout(() => $("chat-input").focus(), 50);
    return;
  }
  loadPermissionMode(id);
  // 先用 home 列表里的已知 session 状态；网络状态走 conn-dot
  const _s = state.sessionsById.get(id);
  if (_s) syncChatStatusFromSession(_s);
  setConnDot("connecting", "Connecting");

  // —— 缓存命中：DOM + state 直接拿出来，免 spinner 免重渲，WS 只补 maxSeq 之后的新事件
  const cached = state.sessionCache.get(id);
  if (cached) {
    state.sessionCache.delete(id);   // LRU：拿出来用，离开时再放回
    restoreSessionCache(cached);
    _installTurnCardMutationDiag();
    _assertNoDupTurnCards("cache-restore");
    // cwd 显示用最新 home 数据（一般不变，但兜底）. 用 abbreviateHome 保留 ~,
    // 由 CSS .meta-cwd dir=rtl 处理截断 — 不能预截 (会丢 ~).
    state.cwdShort = abbreviateHome(cwd || "");
    refreshChatMeta();
    refreshConvStatus();
    state.cacheHit = true;
    // overlay 不显示；立刻揭幕
    $("chat-loading").hidden = true;
    state.revealChat = () => showView("chat");
    state.revealChat();
    restoreChatInputDraft(id);
    if (window.innerWidth >= 900) $("chat-input").focus();
    if (sessionState && sessionState !== "running") {
      try {
        await api(`/api/sessions/${encodeURIComponent(id)}/resume`, { method: "POST" });
      } catch (e) {
        appendBubble("system", `Resume failed: ${e.message}`);
      }
    }
    connectWS();
    return;
  }

  // —— 缓存未命中：原本的冷启动流程
  state.cacheHit = false;
  state.msgById.clear();
  state.toolById.clear();
  state.askuserById.clear();
  state.activeMsgId = null;
  state.blocksByIdx.clear();
  state.firstSeq = null;
  state.hasMoreHistory = false;
  state.maxSeq = 0;
  state.cwdShort = abbreviateHome(cwd || "");
  state.totalCostUsd = 0;
  state.lastInputTokens = 0;
  state.lastInputTotal = 0;
  state.curOutputTokens = 0;
  state.priorTurnOutput = 0;
  state.turnStartAt = null;
  state.turnEndAt = null;
  state.isHistoryReplay = true;
  state.contextLimit = 200_000;
  state.turnFresh = false;
  refreshChatMeta();
  // 立即从 server 读 jsonl 拿上次 ctx，不用等下次 message_start
  api(`/api/sessions/${encodeURIComponent(id)}/ctx`).then(data => {
    if (!data || !data.available) return;
    if (state.sessionId !== id) return;
    state.currentMsgModel = data.model || state.currentMsgModel;
    state.lastInputTokens = data.input_tokens || 0;
    state.lastInputTotal = (data.input_tokens || 0)
                         + (data.cache_read_input_tokens || 0)
                         + (data.cache_creation_input_tokens || 0);
    const m = state.currentMsgModel || "";
    state.contextLimit = /\[1m\]|-1m\b|opus-4-7|opus-4\.7|opus-4-8/i.test(m) ? 1_000_000 : 200_000;
    if (state.lastInputTotal > state.contextLimit) state.contextLimit = 1_000_000;
    refreshChatMeta();
    refreshConvStatus();
  }).catch(() => {});
  $("chat-log").innerHTML = "";
  state.currentToolGroup = null;
  state.earlierFragment = null;
  // dup 诊断 observer — chat-log 任何 .turn-card 增改都立即扫一次.
  _installTurnCardMutationDiag();
  // 进 chat 先显示 spinner 遮挡 chat-log. 后台 backlog + autoFill 渲到
  // chat-log (用户看不见, overlay 盖着). autoFill 满 2 屏后 hide overlay,
  // 用户一次看到完整 2 屏内容.
  const _ld = $("chat-loading");
  _ld.classList.remove("fade-out");
  _ld.hidden = false;

  // §15 enter-latency: reveal chat view IMMEDIATELY — the spinner
  // (#chat-loading) sits in the chat-log so the user sees the transition
  // animation right after click. History and /resume run in the
  // background; backlog_done eventually fades the spinner.
  showView("chat");
  restoreChatInputDraft(id);
  if (window.innerWidth >= 900) $("chat-input").focus();
  state.revealChat = () => {};   // already revealed; first_paint / backlog_done don't need to do it

  // /resume is fire-and-forget — its only side effect (spawning the
  // CLI) is needed for sending messages, not for showing history.
  // WS connect also auto-resumes on first user message anyway.
  if (sessionState && sessionState !== "running") {
    const ownId = id;
    api(`/api/sessions/${encodeURIComponent(id)}/resume`, { method: "POST" })
      .catch(e => {
        if (state.sessionId === ownId) {
          appendBubble("system", `Resume failed: ${e.message}`);
        }
      });
  }

  // IDB replay: 进 chat 时立刻渲上次缓存的 envelopes (0-latency reveal).
  // 设置 state.maxSeq 让 WS open 时 dedupeBoundary=maxSeq, server 推的 backlog
  // 凡 seq <= maxSeq 自动 skip, 只补增量. fire-and-forget — connectWS 不等它.
  (async () => {
    const ownId = id;
    const rows = await idbGetSessionMessages(id);
    if (state.sessionId !== ownId || !rows.length) return;
    // 进 chat-log 已经 innerHTML="" 但可能 WS open 慢点先来, 这里渲缓存
    // 内容. ws 后来的 envelope 走 dedupeBoundary 跳过同 seq.
    for (const r of rows) {
      try {
        handleEvent(r.event, r.ts);
        if (r.seq > (state.maxSeq || 0)) state.maxSeq = r.seq;
      } catch (e) {
        console.warn("idb replay handleEvent failed:", e);
      }
    }
    // IDB replay 跟 WS backlog 可能 race. backlog_done 时的 dedupe 在 IDB
    // 还没跑完时就过了 — IDB 后续 events 可能再造卡. 这里 IDB 收尾再 dedupe
    // 一次兜底.
    _dedupeTurnCardsByKey();
    _assertNoDupTurnCards("after-idb-replay");
    // 立即 fade-out spinner — 缓存内容已经渲, 用户可见. backlog 还会来,
    // 走 dedupe 静默合入.
    const _ld = $("chat-loading");
    if (_ld && !_ld.hidden) {
      _ld.classList.add("fade-out");
      setTimeout(() => {
        _ld.hidden = true;
        _ld.classList.remove("fade-out");
      }, 220);
    }
    // 贴底, scroll snap 到最新
    const log = $("chat-log");
    if (log) setScrollTopInstant(log, log.scrollHeight);
  })();

  connectWS();
}

$("chat-interrupt").addEventListener("click", async () => {
  if (!state.sessionId) return;
  if (!confirm("Interrupt the current session? Running tools will be aborted.")) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/interrupt`, { method: "POST" });
  } catch (e) {
    alert("Interrupt failed: " + (e.message || e));
  }
});

// ---------- 权限模式 ----------
state.permissionMode = "manual";

const VALID_PERM_MODES = ["manual", "accept_edits", "plan", "allow_all"];

// 4 模式 → button title 显示给 hover. CSS .mode-<x> class 切对应 SVG.
const PERM_MODE_LABELS = {
  manual: "Ask each time",
  accept_edits: "Auto edits",
  plan: "Plan only",
  allow_all: "Allow all",
};

function applyPermissionMode(mode) {
  if (!VALID_PERM_MODES.includes(mode)) return;
  state.permissionMode = mode;
  // button class 切到对应 mode (CSS 显示对应那套 SVG); title 跟随刷新,
  // 鼠标 hover 显示当前模式的人类可读名称.
  const btn = document.getElementById("chat-menu-perm-btn");
  if (btn) {
    VALID_PERM_MODES.forEach(m => btn.classList.toggle(`mode-${m}`, m === mode));
    btn.title = `Permission: ${PERM_MODE_LABELS[mode] || mode}`;
  }
  // perm-menu 内当前模式标 .active (打 ✓)
  document.querySelectorAll("#perm-menu .perm-menu-item").forEach(b => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
}

async function loadPermissionMode(sid) {
  try {
    const j = await api(`/api/sessions/${encodeURIComponent(sid)}/permission_mode`);
    if (state.sessionId === sid) applyPermissionMode(j.mode || "manual");
  } catch (e) {
    // 没有也无所谓，按默认 manual 显示
    applyPermissionMode("manual");
  }
}

async function setPermissionMode(mode) {
  if (!state.sessionId) return;
  const prev = state.permissionMode;
  applyPermissionMode(mode);   // 乐观更新
  try {
    await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/permission_mode`, {
      method: "PUT", body: JSON.stringify({ mode }),
    });
  } catch (e) {
    applyPermissionMode(prev);
    alert("Set permission mode failed: " + (e.message || e));
  }
}

// chat 右上角统一菜单 — 4 分区 (perm / model / effort / ctx). 替代旧的
// #chat-perm 按钮 + #perm-menu popover + #chat-ctx-ring + #ctx-tooltip.
function toggleChatMenu(force) {
  const menu = $("chat-menu");
  if (!menu) return;
  const show = (typeof force === "boolean") ? force : menu.hidden;
  menu.hidden = !show;
  if (show) {
    refreshConvStatus();   // 打开时立即同步 ctx 数字
  }
}

const _chatMenuBtn = $("chat-menu-btn");
if (_chatMenuBtn) {
  _chatMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleChatMenu();
  });
}

// 菜单内 perm button + #perm-menu popup: 点 button → toggle menu; 点
// .perm-menu-item → setPermissionMode(mode) + 关 menu. 外部点击也关.
const _chatPermBtn = $("chat-menu-perm-btn");
const _permMenu = $("perm-menu");
// chat-head 内 2 个弹层 (perm-menu / model-menu) 互斥: 打开一个自动关另一个.
function _closeOtherHeadMenu(keep) {
  const pm = document.getElementById("perm-menu");
  const mm = document.getElementById("model-menu");
  if (pm && keep !== "perm") pm.hidden = true;
  if (mm && keep !== "model") mm.hidden = true;
}

if (_chatPermBtn && _permMenu) {
  _chatPermBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = _permMenu.hidden;
    _closeOtherHeadMenu(willOpen ? "perm" : null);
    _permMenu.hidden = !willOpen;
  });
  _permMenu.querySelectorAll(".perm-menu-item").forEach(item => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      const mode = item.dataset.mode;
      if (mode && state.sessionId) setPermissionMode(mode);
      _permMenu.hidden = true;
    });
  });
  document.addEventListener("click", (e) => {
    if (_permMenu.hidden) return;
    if (e.target.closest("#chat-menu-perm-btn")
        || e.target.closest("#perm-menu")) return;
    _permMenu.hidden = true;
  });
}

// 任何弹出菜单 (perm-menu / model-menu) 在窗口失焦 / tab 隐藏时立即关闭.
// 用户拖去别的窗口 / app 时再回来不要看到悬空的菜单.
function _closeAllPopupMenus() {
  const pm = $("perm-menu");
  if (pm && !pm.hidden) pm.hidden = true;
  const mm = $("model-menu");
  if (mm && !mm.hidden) mm.hidden = true;
}
window.addEventListener("blur", _closeAllPopupMenus);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") _closeAllPopupMenus();
});

// 菜单内 model select → PATCH /model_effort (effort 固定空, 不改 sess.effort)
// 菜单内 model button + #model-menu popup (跟 perm 同款交互).
const _chatModelBtn = $("chat-menu-model-btn");
const _modelMenu = $("model-menu");
if (_chatModelBtn && _modelMenu) {
  _chatModelBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = _modelMenu.hidden;
    _closeOtherHeadMenu(willOpen ? "model" : null);
    _modelMenu.hidden = !willOpen;
  });
  _modelMenu.querySelectorAll(".model-menu-item").forEach(item => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!state.sessionId) { _modelMenu.hidden = true; return; }
      const m = item.dataset.model || "";
      applyModelChoice(m);
      _patchModelToServer(m);
      _modelMenu.hidden = true;
    });
  });
  document.addEventListener("click", (e) => {
    if (_modelMenu.hidden) return;
    if (e.target.closest("#chat-menu-model-btn")
        || e.target.closest("#model-menu")) return;
    _modelMenu.hidden = true;
  });
}

const MODEL_LABELS = {
  "": "Default", opus: "Opus", sonnet: "Sonnet", haiku: "Haiku",
};

// 启动时把 Default item 的初始 chip SVG 存下来, refreshConvStatus 切到 tier
// icon 后, 若 cur_model 不识别 (空 / 非 claude 系) 退回此 chip.
(function _stashDefaultModelChipIcon() {
  const defIcon = document.querySelector(
    '.model-menu-item[data-model=""] .model-menu-icon'
  );
  if (defIcon) defIcon.dataset.chipHtml = defIcon.innerHTML;
})();
function applyModelChoice(model) {
  // 切 button title (hover 显示当前选定) + menu item active marker
  const btn = $("chat-menu-model-btn");
  if (btn) {
    const lbl = MODEL_LABELS[model] !== undefined ? MODEL_LABELS[model] : model;
    btn.title = `Model: ${lbl}`;
  }
  document.querySelectorAll("#model-menu .model-menu-item").forEach(b => {
    b.classList.toggle("active", (b.dataset.model || "") === (model || ""));
  });
}

async function _patchModelToServer(model) {
  if (!state.sessionId) return;
  try {
    const r = await api(
      `/api/sessions/${encodeURIComponent(state.sessionId)}/model_effort`,
      { method: "PATCH", body: JSON.stringify({ model }) },
    );
    const sess = state.sessionsById.get(state.sessionId);
    if (sess) {
      sess.model = r.model || "";
      sess.effort = r.effort || "";
    }
    refreshChatMeta();
  } catch (e) {
    console.warn("model update failed", e);
  }
}

// ctx-ring tooltip — 桌面 hover 弹, 触屏一次 tap 弹.
// 桌面: mouseenter show / mouseleave 100ms hideSoon. ring 不可点击, 不
// 可聚焦, 没 focus 边框.
// 触屏: touchstart 直接 show (不依赖 click, 避免 iOS first-tap 弹完又被
// mouseleave hideSoon 立刻收掉). 外部 touch / click 关闭.
(function setupCtxTooltip() {
  const ring = document.getElementById("chat-ctx-ring");
  const tip = document.getElementById("ctx-tooltip");
  if (!ring || !tip) return;
  let hideTimer = null;
  function show() {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    tip.hidden = false;
  }
  function hide() { tip.hidden = true; }
  function hideSoon() {
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 100);
  }
  ring.addEventListener("mouseenter", show);
  ring.addEventListener("mouseleave", hideSoon);
  tip.addEventListener("mouseenter", show);
  tip.addEventListener("mouseleave", hideSoon);
  // 触屏: touchstart 即显示, e.stopPropagation 防被 document outside-touch
  // close handler 立即关掉
  ring.addEventListener("touchstart", (e) => {
    e.stopPropagation();
    show();
  }, { passive: true });
  // 外部 touch / click 关闭 tooltip (触屏没 mouseleave 自动收)
  function onOutside(e) {
    if (tip.hidden) return;
    if (e.target.closest("#chat-ctx-ring") || e.target.closest("#ctx-tooltip")) return;
    hide();
  }
  document.addEventListener("click", onOutside);
  document.addEventListener("touchstart", onOutside, { passive: true });
})();

// Turn-end 通知开关. 从 localStorage 恢复初值. 用户首次勾选 → 触发
// Notification.requestPermission() (浏览器要求用户手势, 不能自动). 拒绝
// 后 checkbox 回弹 + 提示.
state.notifyOnTurnEnd =
  typeof localStorage !== "undefined"
  && localStorage.getItem("ccr.notifyOnTurnEnd") === "1";
const _notifyCb = $("chat-menu-notify");
const _notifyBtn = $("chat-menu-notify-btn");
function _syncNotifyBtnVisual() {
  if (_notifyBtn) _notifyBtn.classList.toggle("off", !state.notifyOnTurnEnd);
  if (_notifyCb) _notifyCb.checked = !!state.notifyOnTurnEnd;
}
_syncNotifyBtnVisual();
if (_notifyBtn) {
  _notifyBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (state.notifyOnTurnEnd) {
      // 关
      state.notifyOnTurnEnd = false;
      localStorage.removeItem("ccr.notifyOnTurnEnd");
      _syncNotifyBtnVisual();
      return;
    }
    // 开 — 申请权限
    if (typeof Notification === "undefined") {
      alert("This browser does not support notifications.\n"
            + "On iOS, install the app to the home screen first.");
      return;
    }
    let perm = Notification.permission;
    if (perm === "default") {
      try { perm = await Notification.requestPermission(); }
      catch (_) { perm = "denied"; }
    }
    if (perm !== "granted") {
      alert("Notifications denied. Enable them in browser settings.");
      _syncNotifyBtnVisual();
      return;
    }
    state.notifyOnTurnEnd = true;
    localStorage.setItem("ccr.notifyOnTurnEnd", "1");
    _syncNotifyBtnVisual();
  });
}

// 点菜单外关菜单 (菜单内 / btn 自身的 click 不算外部)
document.addEventListener("click", (e) => {
  const menu = $("chat-menu");
  if (!menu || menu.hidden) return;
  if (e.target.closest("#chat-menu") || e.target.closest("#chat-menu-btn")) return;
  toggleChatMenu(false);
});

$("chat-back").addEventListener("click", () => {
  // tmp 未发送 session → 清掉 (不写 cache, 不写 db)
  _cleanupTmpSessionIfLeaving(null);
  // 离开 chat 时断 turn-card observer (会被新 session 的 DOM 替换)
  if (state._turnCardObserver) {
    state._turnCardObserver.disconnect();
    state._turnCardObserver = null;
  }
  state._turnCard = null;
  // 退到 home：把当前 session 的 DOM + state 缓存起来，下次进来直接复用
  saveCurrentSessionCache();
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  state.sessionId = null;
  document.body.classList.remove("has-session");
  toggleChatMenu(false);
  $("chat-loading").hidden = true;
  renderSessionList();   // 取消"当前"高亮
  // 退出时让 textarea 等失焦，否则 iOS PWA 软键盘会留在屏幕上
  if (document.activeElement && typeof document.activeElement.blur === "function") {
    document.activeElement.blur();
  }
  enterHome();
});

// 左边缘右滑返回：跟手实时拖 chat 视图，松手按位移判定。仿 iOS 原生手势，窄屏单栏才启用
function installSwipeBack(viewId, opts) {
  const view = $(viewId);
  if (!view) return;
  // opts.commit() 在滑动完成后被调用，作用是触发 view 的退出 (chat: 点
  // ←返回; settings: 移除 .active)。opts.narrowOnly = true 时仅窄屏启用,
  // 跟原 chat 行为一致 — 桌面端有鼠标, 不需要边缘手势。
  // opts.followIds: 额外要跟着 view 一起 transform 的元素 id 列表
  // (例如 chat-head 是 view-chat 的兄弟, fixed 元素, 视觉上要跟着
  // 一起滑动)。
  const narrowOnly = !!opts.narrowOnly;
  const onCommit = opts.commit;
  const followIds = opts.followIds || [];
  const followEls = followIds.map(id => document.getElementById(id))
                              .filter(el => el != null);
  function applyTransform(value) {
    view.style.transform = value;
    for (const el of followEls) el.style.transform = value;
  }
  function applyTransition(value) {
    view.style.transition = value;
    for (const el of followEls) el.style.transition = value;
  }
  function clearInline() {
    view.style.transform = "";
    for (const el of followEls) el.style.transform = "";
  }
  // 起手区扩展到左 60px 以内 — iOS Safari tab 把最左 15-20px 留给自己
  // 的"返回上一页"系统手势, 我们的可触发区往中间挪一些 (15-60px), 既
  // 给系统让位又让用户能在这个 40px 缓冲区里轻松触发我们的 swipe-back.
  // PWA standalone 模式下整个 0-60px 都给我们.
  const EDGE = 60;
  const SLOP = 8;              // 决定方向前的容差
  const COMMIT_FRAC = 0.35;    // 松手时位移超过这个比例 → 继续滑出返回
  const COMMIT_VELOCITY = 0.5; // px/ms，速度足够也直接返回
  let armed = false;           // 起手在边缘内，但还没确定是横向手势
  let dragging = false;        // 已确认是横向手势，跟手中
  let startX = 0, startY = 0, startT = 0, lastX = 0, lastT = 0, width = 0;

  function endTransition(target, onEnd) {
    // 跟手松手后的回弹/滑完用一个偏短的 transition，保持利落
    applyTransition("transform 260ms cubic-bezier(0.25, 1, 0.5, 1)");
    let done = false;
    const fire = () => {
      if (done) return;
      done = true;
      view.removeEventListener("transitionend", fire);
      applyTransition("");
      onEnd && onEnd();
    };
    view.addEventListener("transitionend", fire);
    setTimeout(fire, 320);          // 兜底：transitionend 偶尔不触发
    applyTransform(target);
  }

  view.addEventListener("touchstart", e => {
    if (narrowOnly && window.innerWidth >= 900) return;
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
      if (!opts.silent) applyTransition("none");
    }
    // 一旦确认是右滑返回手势，吃掉所有 touchmove (silent 模式也吃,
    // 这才是阻止 iOS 系统 swipe-back 的关键).
    if (e.cancelable) e.preventDefault();
    if (!opts.silent) {
      const tx = Math.max(0, dx);
      applyTransform(`translateX(${tx}px)`);
    }
    lastX = t.clientX;
    lastT = e.timeStamp;
  }, { passive: false });

  function release(e) {
    if (!armed) return;
    armed = false;
    if (!dragging) return;
    dragging = false;
    // silent 模式: 手势已经被 touchmove 吃掉, 视觉上没动过, 直接收工.
    if (opts.silent) return;
    const t = (e.changedTouches && e.changedTouches[0]) || { clientX: lastX, timeStamp: lastT };
    const dx = t.clientX - startX;
    const dt = Math.max(1, (e.timeStamp || lastT) - lastT);
    const v = (t.clientX - lastX) / dt;  // px/ms，最后一段速度
    // bounceOnly view (例如 home — 已是栈底, 没有上一层可退): 永远回弹,
    // 但 touchmove 已经 preventDefault 吃掉手势, 不让 iOS 走系统 swipe-back.
    const commit = !opts.bounceOnly &&
                   (dx > width * COMMIT_FRAC || v > COMMIT_VELOCITY);
    if (commit) {
      endTransition(`translateX(${width}px)`, () => {
        onCommit && onCommit();
        clearInline();
      });
    } else {
      endTransition("translateX(0)", () => { clearInline(); });
    }
  }
  view.addEventListener("touchend", release, { passive: true });
  view.addEventListener("touchcancel", () => {
    if (dragging) endTransition("translateX(0)", () => { clearInline(); });
    armed = false; dragging = false;
  }, { passive: true });
}

// view-home 是栈底 view, 没有"上一层"可退. silent swipe-back: touchmove
// preventDefault 吃掉手势 (阻止 iOS PWA 系统右滑触发 history.back),
// 但视觉上完全不响应 — 用户右滑像没发生过.
installSwipeBack("view-home", {
  narrowOnly: true,
  silent: true,
  commit: () => {},
});

installSwipeBack("view-chat", {
  narrowOnly: true,
  commit: () => $("chat-back").click(),
  // chat-head 是 view-chat 的兄弟 (fixed), 但视觉上要跟着一起滑动 ——
  // 否则手指拖 view 时 head 静止, 看起来跟头"脱节".
  followIds: ["chat-head"],
});
installSwipeBack("view-settings", {
  // 设置页在所有屏幕都是全屏 overlay, 桌面端也支持触屏右滑退出
  narrowOnly: false,
  commit: () => $("view-settings").classList.remove("active"),
});
installSwipeBack("view-apps", {
  narrowOnly: false,
  commit: () => $("view-apps").classList.remove("active"),
});
installSwipeBack("view-help", {
  narrowOnly: false,
  commit: () => {
    $("view-help").classList.remove("active");
    if (location.hash === "#help") {
      history.replaceState(null, "", location.pathname + location.search);
    }
  },
});

// ---------- PWA / 浏览器 back-gesture 接管 ----------
// iOS PWA + Android Chrome 默认边缘右滑 / 系统返回会把用户踢出 SPA,
// 即使我们已经 installSwipeBack 监听了 touch event — 因为浏览器 / 系统
// 在 touch 路径外有自己的 history.back() 触发. 唯一 100% 解法: 维护一个
// 永远在栈顶的 anchor state, 每次 popstate 触发就先自己 close 一个 view,
// 再 pushState 把 anchor 推回去, 用户永远 back 不出去.
//
// 关闭优先级 (从内到外):
//   1) help / settings / apps / browse modal / new-app panel 等 overlay
//   2) chat view (has-session) → 退到 home
//   3) 各种 popup menu (chat-menu / model-menu / perm-menu)
//   4) 都没 → 啥都不做 (浏览器已被 anchor 兜底)
(function setupBackInterception() {
  const ANCHOR_BASE = { _ccrAnchor: "base" };
  const ANCHOR = { _ccrAnchor: "top" };
  // iOS PWA standalone 模式启动时, history 栈底那个 entry 有可能是
  // about:blank (start_url 加载前的占位). 用户右滑就直接退到空白页.
  // 用 replaceState 把启动 entry 改写成自己 (URL 强制 = 当前页), 再
  // pushState 多层 ANCHOR — 给浏览器一个 buffer, 即便 anchor 被吃掉,
  // 栈底也还是自己; 即便 iOS 视觉过渡比 JS pushState 快, 多 push 几层
  // 也能确保用户连续右滑也不会捅穿.
  try { history.replaceState(ANCHOR_BASE, "", location.href); } catch (_) {}
  for (let i = 0; i < 5; i++) {
    try { history.pushState(ANCHOR, "", location.href); } catch (_) {}
  }

  function closeOneLayer() {
    // 优先: 任何 overlay view 处于 active
    const help = $("view-help");
    if (help && help.classList.contains("active")) {
      help.classList.remove("active");
      if (location.hash === "#help") {
        try { history.replaceState(null, "",
          location.pathname + location.search); } catch (_) {}
      }
      return true;
    }
    const apps = $("view-apps");
    if (apps && apps.classList.contains("active")) {
      apps.classList.remove("active"); return true;
    }
    const settings = $("view-settings");
    if (settings && settings.classList.contains("active")) {
      settings.classList.remove("active"); return true;
    }
    // 弹层 menu
    const popups = ["model-menu", "perm-menu", "chat-menu"];
    for (const id of popups) {
      const el = document.getElementById(id);
      if (el && !el.hidden) { el.hidden = true; return true; }
    }
    // 内嵌 modals (new-session / browse / new-app panel)
    const modals = ["modal-new-session", "modal-browse"];
    for (const id of modals) {
      const el = document.getElementById(id);
      if (el && !el.hasAttribute("hidden")) {
        el.setAttribute("hidden", ""); return true;
      }
    }
    const newAppPanel = $("new-app-panel");
    if (newAppPanel && !newAppPanel.hasAttribute("hidden")) {
      newAppPanel.setAttribute("hidden", ""); return true;
    }
    // chat view → 退到 home (走 chat-back 同款逻辑)
    if (document.body.classList.contains("has-session")) {
      const back = $("chat-back");
      if (back) { back.click(); return true; }
    }
    return false;
  }

  window.addEventListener("popstate", (e) => {
    // 先把 anchor 推回去 — 缩短 "栈深度 -1" 的 gap, 避免用户在 ~100ms
    // 内连滑两下时第二下退到栈底 (即使有 ANCHOR_BASE 兜底也少绕一层).
    try { history.pushState(ANCHOR, "", location.href); } catch (_) {}
    // login view 时不接管 (用户可能想离开网站 — 但因为 ANCHOR_BASE 在
    // 栈底, 实际效果是再多滑一下才能退出, 不会瞬间 blank).
    const inLogin = document.body.classList.contains("stage-login");
    if (!inLogin) closeOneLayer();
  });
})();

function setConnDot(kind, title) {
  const el = $("conn-dot");
  if (!el) return;
  el.className = "conn-dot" + (kind ? " " + kind : "");
  el.title = title || "";
}

function setStatus(cls, text) {
  const el = $("chat-status");
  el.classList.remove("busy", "error");
  if (cls === "busy") el.classList.add("busy");
  if (cls === "error") el.classList.add("error");
  // 顶部只在 error 时显示文本；"工作中" / "空闲" / "完成" 不显示文字
  el.textContent = (cls === "error") ? text : "";
  // 终止按钮只在跟模型交互（busy）时显示在 chat-head
  const btn = $("chat-interrupt");
  if (btn) btn.hidden = cls !== "busy";
  // 思考中状态点
  state.isBusy = (cls === "busy");
  refreshConvStatus();
}

// 时间格式：x s / x m xx s / x h xx m xx s（单位前后留空格）
function formatDuration(sec) {
  sec = Math.max(0, sec | 0);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) {
    const m = (sec / 60) | 0, s = sec % 60;
    return `${m}m${s}s`;
  }
  const h = (sec / 3600) | 0, m = ((sec % 3600) / 60) | 0, s = sec % 60;
  return `${h}h${m}m${s}s`;
}

// Claude Code CLI 风格的 thinking 星号循环帧
const CC_SPARK_FRAMES = ["·", "✢", "✳", "✴", "✻", "✽"];
let _ccSparkIdx = 0;
let _ccSparkTimer = null;
function startSparkAnim() {
  if (_ccSparkTimer) return;
  _ccSparkTimer = setInterval(() => {
    const el = $("cs-dot");
    if (!el) return;
    _ccSparkIdx = (_ccSparkIdx + 1) % CC_SPARK_FRAMES.length;
    el.textContent = CC_SPARK_FRAMES[_ccSparkIdx];
  }, 220);
}
function stopSparkAnim() {
  if (_ccSparkTimer) { clearInterval(_ccSparkTimer); _ccSparkTimer = null; }
}

// Turn card — 跟随当前 turn 流的特殊卡, 显示闪烁 model icon + ↓token +
// duration. turn 开始时创建, 进行中 refreshConvStatus 同步刷新, turn 结束
// (state.turnEndAt 从 null 变非空) 时 finalize: 移除 .turn-active class
// (icon 停闪), 断 MutationObserver (不再粘 chat-log 末尾).
//
// state._turnCard: 当前 active 或 finalized 的 card DOM (跨 turn 保留引用,
//   下次 turn 开始时检查 .turn-active 决定是否复用).
// state._turnCardObserver: 推 card 回末尾的 MutationObserver, 仅 active 期间存在.

// 架构: turn-card 用 data-turn-start (turnStartAt 毫秒) 作幂等键. 实时
// lifecycle (_ensureTurnCard / _finalizeTurnCard) 和持久化回放 (_renderTurnSummary)
// 写入前**先扫 chat-log 删除 / 复用同键 card**, 跟事件到达顺序无关. 即使
// turn_state + turn_summary 时序错乱 / 重复也只剩 1 张.
function _turnStartKey() {
  return state.turnStartAt ? String(state.turnStartAt) : "";
}
function _findTurnCardsByKey(root, key) {
  if (!root || !key) return [];
  return Array.from(
    root.querySelectorAll(`:scope > .turn-card[data-turn-start="${key}"]`)
  );
}

// 跨 root 搜同 key turn-card — chat-log + earlierFragment 都看. 修复
// IDB replay (chatRoot=chat-log) + WS earlier batch (chatRoot=earlierFragment)
// 跨 root 各建一张 dedupe miss 的问题. 返回所有找到的卡, 调用方决定接管哪张.
function _findTurnCardsByKeyAcrossRoots(key) {
  if (!key) return [];
  const log = $("chat-log");
  const cards = [];
  if (log) {
    log.querySelectorAll(
      `:scope > .turn-card[data-turn-start="${key}"]`
    ).forEach(c => cards.push(c));
  }
  if (state.earlierFragment) {
    state.earlierFragment.querySelectorAll(
      `:scope > .turn-card[data-turn-start="${key}"]`
    ).forEach(c => cards.push(c));
  }
  return cards;
}

// 推断 model brand — Anthropic 系细分到 tier (opus/sonnet/haiku), 其它 LLM
// 厂家粒度即可 (品牌 logo 区分度足够). 顺序敏感: 第一个匹配 substring 命中即返回.
// 图标来源: simple-icons SVG 已下到 static/lib/llm-icons/<slug>.svg + SW 预 cache,
// 不走 CDN 减少首次延迟 + 支持离线.
const _MODEL_BRANDS = [
  // Anthropic — 细分 tier (现有 .model-menu-item icon 复用)
  { match: ["opus"],                tier: "opus" },
  { match: ["sonnet"],              tier: "sonnet" },
  { match: ["haiku"],               tier: "haiku" },
  // 其它大厂 — 本地 svg 文件名 + brand color (color 已 baked 进 svg)
  { match: ["deepseek"],            tier: "deepseek", slug: "deepseek", boost: 1.3 },
  { match: ["gemini"],              tier: "gemini",   slug: "googlegemini" },
  { match: ["gpt", "openai", "o3", "o4", "o1"],
                                    tier: "openai",   slug: "_openai_inline" },
  { match: ["qwen"],                tier: "qwen",     slug: "qwen" },
  { match: ["llama"],               tier: "llama",    slug: "meta" },
  { match: ["mistral"],             tier: "mistral",  slug: "mistralai" },
  { match: ["grok"],                tier: "grok",     slug: "x" },
  { match: ["kimi", "moonshot"],    tier: "kimi",     slug: "moonshotai", boost: 1.2 },
  { match: ["doubao"],              tier: "doubao",   slug: "bytedance" },
  { match: ["phi", "wizardlm", "copilot"],
                                    tier: "copilot",  slug: "githubcopilot" },
  { match: ["glm", "zhipu"],        tier: "glm",      slug: "alibabadotcom" },
  // 兜底 Anthropic Claude
  { match: ["claude", "anthropic"], tier: "claude",   slug: "claude" },
];
// OpenAI 无 simple-icons (商标策略). 自己 inline 一个简化"六瓣花"近似形, 紫色填充.
const _OPENAI_INLINE_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="#412991" aria-hidden="true"><path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.759a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.5 4.5 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-4.83-2.786A4.504 4.504 0 0 1 2.34 7.872zm16.597 3.855l-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l4.83 2.791a4.494 4.494 0 0 1-.676 8.105v-5.678a.79.79 0 0 0-.407-.667zm2.01-3.023l-.141-.085-4.774-2.782a.776.776 0 0 0-.785 0L9.409 9.23V6.897a.066.066 0 0 1 .028-.061l4.83-2.787a4.5 4.5 0 0 1 6.68 4.66zm-12.64 4.135l-2.02-1.164a.08.08 0 0 1-.038-.057V6.075a4.5 4.5 0 0 1 7.375-3.453l-.142.08L8.704 5.46a.795.795 0 0 0-.393.681zm1.097-2.365l2.602-1.5 2.607 1.5v2.999l-2.597 1.5-2.607-1.5z"/></svg>`;
function _tierFromModel(model) {
  const lower = (model || "").toLowerCase();
  if (!lower) return "";
  for (const b of _MODEL_BRANDS) {
    if (b.match.some(s => lower.includes(s))) return b.tier;
  }
  return "other";
}
// 通用 spark fallback (currentColor 走父级 icon 的颜色).
const _OTHER_TIER_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 13.5 10.5 21 12 13.5 13.5 12 21 10.5 13.5 3 12 10.5 10.5z"/></svg>`;
function _iconHTMLForTier(tier) {
  if (!tier) return "";
  if (tier === "other") return _OTHER_TIER_SVG;
  // Anthropic 系优先用已存在的 .model-menu-icon SVG (跟现有 menu 视觉一致)
  const menuIcon = document.querySelector(
    `.model-menu-item[data-model="${tier}"] .model-menu-icon`,
  );
  if (menuIcon && menuIcon.innerHTML) return menuIcon.innerHTML;
  const brand = _MODEL_BRANDS.find(b => b.tier === tier);
  if (!brand || !brand.slug) return _OTHER_TIER_SVG;
  if (brand.slug === "_openai_inline") return _OPENAI_INLINE_SVG;
  // 本地 <img>, SW 预 cache, 首次/离线都无延迟. boost 给个别 brand 单独
  // 放大 (e.g. deepseek 实际占 viewBox 比例小, 视觉偏小, +30%).
  const base = new URL("./", document.baseURI).pathname;
  const px = Math.round(14 * (brand.boost || 1));
  return `<img src="${base}static/lib/llm-icons/${brand.slug}.svg" alt="${tier}" width="${px}" height="${px}" style="display:block">`;
}

function _ensureTurnCard() {
  const key = _turnStartKey();
  if (state._turnCard
      && state._turnCard.isConnected
      && state._turnCard.classList.contains("turn-active")
      && (!key || state._turnCard.dataset.turnStart === key)) {
    return state._turnCard;
  }
  const log = $("chat-log");
  if (!log) return null;
  // 同 turn 已有 card (实时 active 或 turn_summary 早到先渲了 finalized) — 接管.
  // 多余的同 key card 直接删, 保留一张. 跨 root 搜 (chat-log + earlierFragment)
  // 防 IDB replay vs WS earlier batch 跨 root 各建一张漏 dedupe.
  let card = null;
  if (key) {
    const sameKey = _findTurnCardsByKeyAcrossRoots(key);
    if (sameKey.length) {
      // 优先选 active 的, 其次 finalized; 多余删掉.
      card = sameKey.find(c => c.classList.contains("turn-active")) || sameKey[0];
      sameKey.forEach(c => { if (c !== card) c.remove(); });
      card.classList.add("turn-active");
    }
  }
  if (!card) {
    // 退路: 无 key 时接管 DOM 末尾任何 active card (e.g. cache restore).
    card = log.querySelector(":scope > .turn-card.turn-active");
  }
  if (!card) {
    card = document.createElement("div");
    card.className = "turn-card turn-active";
    card.innerHTML = `<span class="turn-card-icon"></span>`
      + `<span class="turn-card-tokens">↓0t</span>`
      + `<span class="turn-card-time">0s</span>`;
    log.appendChild(card);
  }
  if (key) card.dataset.turnStart = key;
  state._turnCard = card;
  if (state._turnCardObserver) state._turnCardObserver.disconnect();
  state._turnCardObserver = new MutationObserver(() => {
    const c = state._turnCard;
    if (!c || !c.classList.contains("turn-active")) return;
    const lg = $("chat-log");
    if (lg && lg.lastElementChild !== c) lg.appendChild(c);
  });
  state._turnCardObserver.observe(log, { childList: true });
  return card;
}

function _refreshTurnCard() {
  // 自动兜底 (仅 live 模式):
  //   turnStartAt 有 + turnEndAt 无 (进行中) → ensure active card
  //   turnStartAt 有 + turnEndAt 有 (已结束) + 没 card → 接管 / 新建 finalized
  //
  // ★ replay 期间 (isHistoryReplay=true) 跳过结构调整 — 历史 turn 由
  //   turn_summary 走 _renderTurnSummary 进 earlierFragment 建卡;
  //   最新 turn 的 snapshot 卡由 first_paint handler 直接调
  //   _renderTurnSummary / _ensureTurnCard 建. 否则 replay 期间每轮
  //   applyTurnState → refreshConvStatus 又调过来, 给每个老 turn 在 chat-log
  //   末尾再造一张, 跟 earlierFragment 里同 key 的卡叠在一起 (现象: 末尾
  //   多张相同 turn-card).
  if (!state.isHistoryReplay && state.turnStartAt) {
    const shouldBeActive = !state.turnEndAt;
    const cardOk = state._turnCard && state._turnCard.isConnected;
    if (shouldBeActive) {
      if (!cardOk || !state._turnCard.classList.contains("turn-active")) {
        _ensureTurnCard();
      }
    } else if (!cardOk) {
      // turn 已结束 + state._turnCard 没 (e.g. 强刷). 先看 chat-log 内是否
      // 已有 turn_summary event 渲的 finalized card. 有就接管, 不重复创建.
      const log = $("chat-log");
      const existing = log
        && log.querySelectorAll(":scope > .turn-card:not(.turn-active)");
      if (existing && existing.length) {
        state._turnCard = existing[existing.length - 1];
      } else {
        _ensureTurnCard();
        _finalizeTurnCard();
      }
    }
  }
  const card = state._turnCard;
  if (!card || !card.isConnected) return;
  const tokensEl = card.querySelector(".turn-card-tokens");
  const timeEl = card.querySelector(".turn-card-time");
  const iconEl = card.querySelector(".turn-card-icon");
  if (tokensEl) {
    const tok = state.curOutputTokens || 0;
    tokensEl.textContent = `↓${_fmtTok(tok)}`;
  }
  if (timeEl && state.turnStartAt) {
    const end = state.turnEndAt || Date.now();
    const elapsed = Math.max(0, Math.round((end - state.turnStartAt) / 1000));
    timeEl.textContent = formatDuration(elapsed);
  }
  if (iconEl) {
    const curM = state.currentMsgModel || "";
    const tier = _tierFromModel(curM);
    if (iconEl.dataset.tier !== tier) {
      iconEl.dataset.tier = tier;
      iconEl.innerHTML = _iconHTMLForTier(tier);
    }
  }
}

function _finalizeTurnCard() {
  const card = state._turnCard;
  if (!card) return;
  card.classList.remove("turn-active");
  if (state._turnCardObserver) {
    state._turnCardObserver.disconnect();
    state._turnCardObserver = null;
  }
}

// 诊断: 检查 chat-log 是否有同 key 的 turn-card. 有就 console.error
// + 在 UI 顶部弹个可点的红色 banner (累计计数 + 一键 copy 完整 JSON
// 到剪贴板). 不抛, 不阻塞. _dupDiagBuffer 在 enterChat 清零.
const _dupDiagBuffer = [];
let _dupDiagBannerEl = null;

function _cardSnap(c, log) {
  return {
    idx: Array.from(log.children).indexOf(c),
    key: c.dataset.turnStart || null,
    active: c.classList.contains("turn-active"),
    tokens: c.querySelector(".turn-card-tokens")?.textContent || "",
    time: c.querySelector(".turn-card-time")?.textContent || "",
    iconTier: c.querySelector(".turn-card-icon")?.dataset.tier || "",
  };
}

function _showDupDiagBanner() {
  if (!_dupDiagBannerEl) {
    const el = document.createElement("div");
    el.id = "dup-diag-banner";
    el.style.cssText =
      "position:fixed;top:12px;right:12px;z-index:9999;"
      + "background:#cf222e;color:#fff;padding:8px 14px;border-radius:8px;"
      + "font:600 13px ui-monospace,SFMono-Regular,Menlo,monospace;"
      + "box-shadow:0 6px 18px rgba(0,0,0,0.25);cursor:pointer;"
      + "max-width:380px;line-height:1.4;";
    el.title = "Click to copy full diagnostic JSON to clipboard";
    el.addEventListener("click", _copyDupDiag);
    document.body.appendChild(el);
    _dupDiagBannerEl = el;
  }
  _dupDiagBannerEl.textContent =
    "🐛 turn-card dup × " + _dupDiagBuffer.length + " (click to copy diag)";
}

function _hideDupDiagBanner() {
  if (_dupDiagBannerEl) {
    _dupDiagBannerEl.remove();
    _dupDiagBannerEl = null;
  }
}

async function _copyDupDiag() {
  const log = $("chat-log");
  const sid = state.sessionId;
  const sess = sid ? state.sessionsById.get(sid) : null;
  const data = {
    ts: new Date().toISOString(),
    appVer: typeof __CCR_APP_VER !== "undefined" ? __CCR_APP_VER : "?",
    sessionId: sid,
    sessionState: sess && sess.state || "?",
    appOnline: sess && sess.app_online,
    appId: sess && sess.app_id,
    isHistoryReplay: state.isHistoryReplay,
    earlierFragmentSet: !!state.earlierFragment,
    maxSeq: state.maxSeq,
    dedupeBoundary: state.dedupeBoundary,
    cacheHit: state.cacheHit,
    turnStartAt: state.turnStartAt,
    turnEndAt: state.turnEndAt,
    currentMsgModel: state.currentMsgModel,
    dupEvents: _dupDiagBuffer,
    currentCards: log
      ? Array.from(log.querySelectorAll(":scope > .turn-card"))
          .map(c => _cardSnap(c, log))
      : [],
  };
  const json = JSON.stringify(data, null, 2);
  try {
    await navigator.clipboard.writeText(json);
    if (_dupDiagBannerEl) {
      _dupDiagBannerEl.textContent = "✓ copied — paste to dev";
      setTimeout(() => {
        if (_dupDiagBannerEl) _showDupDiagBanner();
      }, 2000);
    }
  } catch (e) {
    console.error("[CCR] copy diag failed", e);
    console.log("[CCR] diag JSON:\n" + json);
    alert("Clipboard copy failed. Full JSON dumped to console.");
  }
}

function _assertNoDupTurnCards(hint) {
  const log = $("chat-log");
  if (!log) return;
  const seen = new Map();
  log.querySelectorAll(":scope > .turn-card").forEach(c => {
    const k = c.dataset.turnStart || "(no-key)";
    if (seen.has(k)) {
      const first = seen.get(k);
      const info = {
        hint,
        key: k,
        first: _cardSnap(first, log),
        second: _cardSnap(c, log),
        totalCards: log.querySelectorAll(":scope > .turn-card").length,
        isHistoryReplay: state.isHistoryReplay,
        earlierFragmentSet: !!state.earlierFragment,
        turnStartAt: state.turnStartAt,
        turnEndAt: state.turnEndAt,
      };
      console.error("[CCR] DUPLICATE turn-card", info);
      _dupDiagBuffer.push(info);
      _showDupDiagBanner();
    } else {
      seen.set(k, c);
    }
  });
}

// Live observer: chat-log 任何 .turn-card 增减都立即扫一次. 配合
// MutationObserver, dup 出现的那一刻就被捕获 — hint 记成 "live-mutation".
let _turnCardMutationObserver = null;
function _installTurnCardMutationDiag() {
  if (_turnCardMutationObserver) return;
  const log = $("chat-log");
  if (!log) return;
  _turnCardMutationObserver = new MutationObserver((mutations) => {
    let touched = false;
    for (const m of mutations) {
      for (const n of m.addedNodes) {
        if (n.nodeType === 1 && n.classList && n.classList.contains("turn-card")) {
          touched = true; break;
        }
      }
      if (touched) break;
    }
    if (touched) _assertNoDupTurnCards("live-mutation");
  });
  _turnCardMutationObserver.observe(log, { childList: true });
}

// 兜底去重: chat-log + earlierFragment prepend 完成后, 按 dataset.turnStart
// 唯一化 — 同 key 多张卡只留一张 (优先留 active 的, 否则留先到的).
// 防 applyTurnState (live path) 跟 _renderTurnSummary (replay path) 对
// 同一 turn 双写 / 跨 root 漏 dedupe.
function _dedupeTurnCardsByKey() {
  const log = $("chat-log");
  if (!log) return;
  const seen = new Map();   // key → card to keep
  log.querySelectorAll(":scope > .turn-card").forEach(c => {
    const k = c.dataset.turnStart;
    if (!k) return;
    if (seen.has(k)) {
      const first = seen.get(k);
      const firstActive = first.classList.contains("turn-active");
      const cActive = c.classList.contains("turn-active");
      if (cActive && !firstActive) {
        first.remove();
        seen.set(k, c);
      } else {
        c.remove();
      }
    } else {
      seen.set(k, c);
    }
  });
}

// 在 backlog_done 时调一次: 如果 session 不在 busy/running 状态, 任何
// .turn-active 都应该被强制 finalize. 主要兜底数据完整性问题 — 一个
// session 被 kill / 异常退出 时, 最后一轮的 turn_state(end) 没写到 jsonl
// 里, 重放完 state.turnEndAt 仍 null → _ensureTurnCard 把 first_paint
// 卡 re-activate, 没后续 end 事件 finalize. 结果: 末尾两张卡, 一张
// finalized (上一轮) + 一张 active (这一轮假活).
function _reconcileTurnCardsAfterBacklog() {
  const sid = state.sessionId;
  if (!sid) return;
  const sess = state.sessionsById.get(sid);
  const isActive = sess && (sess.state === "busy" || sess.state === "running");
  if (isActive) return;   // session 真活跃, 不动 — 之后 turn_state 会管
  const log = $("chat-log");
  if (!log) return;
  const stragglers = log.querySelectorAll(":scope > .turn-card.turn-active");
  if (!stragglers.length) return;
  stragglers.forEach(c => c.classList.remove("turn-active"));
  // 同步 state: turnEndAt 补一个兜底, 避免后续 _refreshTurnCard 仍判
  // shouldBeActive=true 又把它 re-activate.
  if (state.turnStartAt && !state.turnEndAt) {
    state.turnEndAt = state.turnStartAt;
  }
  // 断 MutationObserver (active 没了, 不需要再把卡推回末尾)
  if (state._turnCardObserver) {
    state._turnCardObserver.disconnect();
    state._turnCardObserver = null;
  }
}

// 渲染 server 推过来的 turn_summary event (已结束 turn 的存档). 不创建活
// card, 直接 append 一张 finalized .turn-card 到 chatRoot() (回放期间 chatRoot
// 是 earlierFragment, 实时是 chat-log). 这样强刷 / backlog 回放也能恢复
// 历史轮次的 token + duration + model icon, 跟时间线其它消息按 seq 顺序排.
function _renderTurnSummary(evt) {
  const root = chatRoot();
  if (!root) return;
  const tok = evt.output_tokens || 0;
  const startMs = evt.turn_started_at ? Math.round(evt.turn_started_at * 1000) : null;
  const endMs = evt.turn_ended_at ? evt.turn_ended_at * 1000 : null;
  const duration = (startMs && endMs)
    ? Math.max(0, Math.round((endMs - startMs) / 1000))
    : 0;
  const key = startMs ? String(startMs) : "";
  // 幂等: 同 turn 已有 card (无论实时建的 active 还是别处先渲的 finalized) —
  // 接管最末一张, 多余的删, 然后 finalize + 刷新数据. 跨 root 搜 (chat-log
  // + earlierFragment) 防 IDB replay vs WS earlier batch 漏 dedupe.
  let card = null;
  if (key) {
    const sameKey = _findTurnCardsByKeyAcrossRoots(key);
    if (sameKey.length) {
      card = sameKey[sameKey.length - 1];
      sameKey.forEach(c => { if (c !== card) c.remove(); });
    }
  }
  const curM = evt.model || "";
  const tier = _tierFromModel(curM);
  const iconHTML = _iconHTMLForTier(tier);
  if (card) {
    // 复用现有 card: 移除 active class, 刷新 token / duration / icon.
    card.classList.remove("turn-active");
    const iconEl = card.querySelector(".turn-card-icon");
    if (iconEl) { iconEl.dataset.tier = tier; iconEl.innerHTML = iconHTML; }
    const tokEl = card.querySelector(".turn-card-tokens");
    if (tokEl) tokEl.textContent = `↓${_fmtTok(tok)}`;
    const timeEl = card.querySelector(".turn-card-time");
    if (timeEl) timeEl.textContent = formatDuration(duration);
  } else {
    card = document.createElement("div");
    card.className = "turn-card";   // 已 finalize, 无 .turn-active
    card.innerHTML =
      `<span class="turn-card-icon">${iconHTML}</span>`
      + `<span class="turn-card-tokens">↓${_fmtTok(tok)}</span>`
      + `<span class="turn-card-time">${formatDuration(duration)}</span>`;
    root.appendChild(card);
  }
  if (key) card.dataset.turnStart = key;
  // 实时路径下 (state._turnCard 是本轮 active) 同步引用; observer 也断,
  // 这张已 finalized 不再粘末尾.
  if (state._turnCard === card || !state._turnCard
      || !state._turnCard.isConnected) {
    state._turnCard = card;
    if (state._turnCardObserver) {
      state._turnCardObserver.disconnect();
      state._turnCardObserver = null;
    }
  }
}

function refreshConvStatus() {
  const dot = $("cs-dot");
  if (dot) {
    const busy = !!state.isBusy;
    const shown = !!state.turnStartAt;   // 第一轮对话开始后一直显示，session 切换才清掉
    dot.classList.toggle("shown", shown);
    dot.classList.toggle("pending", busy && shown);
    if (busy && shown) {
      startSparkAnim();
    } else {
      stopSparkAnim();
      if (shown) dot.textContent = CC_SPARK_FRAMES[4];   // 停止后固定第 5 个 ✻
    }
  }
  const outT = state.curOutputTokens || 0;
  // 显示规则：刚进 session 且本轮已结束 → 不显示，等下一次新轮次。
  // 只有进行中 (turnEndAt 为空) 或本次进 session 后亲眼看到过 turn 动作 (turnFresh) 才显示
  const turnVisible = !!state.turnStartAt && (!state.turnEndAt || !!state.turnFresh);
  const tokens = $("cs-tokens");
  if (tokens) {
    if (turnVisible) {
      tokens.textContent = `↓${_fmtTok(outT)}`;
      tokens.hidden = false;
    } else {
      tokens.textContent = "";
      tokens.hidden = true;
    }
  }
  const time = $("cs-time");
  if (time) {
    if (turnVisible) {
      // 进行中（无 turnEndAt）实时 now；完成后停在 turnEndAt
      const end = state.turnEndAt || Date.now();
      const elapsed = Math.max(0, Math.round((end - state.turnStartAt) / 1000));
      if (elapsed >= 1) {
        time.textContent = formatDuration(elapsed);
        time.hidden = false;
      } else {
        time.hidden = true;
      }
    } else {
      time.textContent = "";
      time.hidden = true;
    }
  }
  const ctx = $("cs-ctx");
  let _ctxPct = 0;
  let _ctxHasData = false;
  if (ctx) {
    const total = state.lastInputTotal || state.lastInputTokens || 0;
    if (total) {
      _ctxHasData = true;
      _ctxPct = total / (state.contextLimit || 200000);
      ctx.textContent = "ctx: " + (_ctxPct * 100).toFixed(1) + "%";
    } else {
      ctx.textContent = "";
    }
  }
  // chat-head 右上角 #chat-menu 弹层 (窄屏) / inline (宽屏) 同步.
  // ctx 用 SVG 圆环 #chat-ctx-ring 显示进度 (跟之前一模一样), hover/click
  // 弹 #ctx-tooltip 显示 pct + detail. perm/model select + notify checkbox
  // 都跟既有 state 同步.
  const menu = document.getElementById("chat-menu");
  if (menu) {
    const pct = _ctxHasData ? Math.min(1, Math.max(0, _ctxPct)) : 0;
    // ring stroke-dashoffset: C = 2πr = 2π·10 ≈ 62.83. fill 描出 pct 部分.
    const ring = document.getElementById("chat-ctx-ring");
    if (ring) {
      const C = 2 * Math.PI * 10;
      const fill = ring.querySelector(".fill");
      if (fill) fill.style.strokeDashoffset = (C * (1 - pct)).toFixed(2);
      ring.classList.toggle("hot",  pct >= 0.85);
      ring.classList.toggle("warm", pct >= 0.6 && pct < 0.85);
    }
    // tooltip 内容 (hover/click ring 时显示)
    const tipPct = document.getElementById("ctx-tooltip-pct");
    const tipDetail = document.getElementById("ctx-tooltip-detail");
    if (tipPct && tipDetail) {
      if (_ctxHasData) {
        const total = state.lastInputTotal || 0;
        const limit = state.contextLimit || 200000;
        tipPct.textContent = (pct * 100).toFixed(1) + "%";
        tipDetail.textContent = `${_fmtCtx(total)} / ${_fmtCtx(limit)}`;
      } else {
        tipPct.textContent = "—";
        tipDetail.textContent = "no data yet";
      }
    }
    // model button + popup menu 同步 (effort UI 已移除).
    const { model } = _currentSessionMeta();
    applyModelChoice(model || "");
    // Default item 的 sub 文本 + icon 跟随实跑 model. cur_model 含 opus/
    // sonnet/haiku 字样 → 复用对应 tier 的 SVG (王冠 / 星 / 羽毛). 不识别
    // 时退回原 chip icon.
    const sid = state.sessionId;
    const s = sid ? state.sessionsById.get(sid) : null;
    const curM = (s && s.cur_model) || state.currentMsgModel || "";
    const defSub = document.getElementById("model-menu-sub-default");
    if (defSub) {
      const newText = curM ? `CLI picks · ${curM}` : "CLI picks";
      if (defSub.textContent !== newText) defSub.textContent = newText;
    }
    const defIcon = document.querySelector(
      '.model-menu-item[data-model=""] .model-menu-icon'
    );
    if (defIcon) {
      const tier = _tierFromModel(curM);
      const cur = defIcon.dataset.tier || "";
      if (tier !== cur) {
        defIcon.dataset.tier = tier;
        if (tier) {
          defIcon.innerHTML = _iconHTMLForTier(tier);
        } else if (defIcon.dataset.chipHtml) {
          defIcon.innerHTML = defIcon.dataset.chipHtml;
        }
      }
    }
    // 自定义 endpoint (非 Claude 原生): 隐藏 opus/sonnet/haiku 子项 — 用户
    // 选了也没用 (USTC / 本地 gateway 不响应 Anthropic alias). 仅 Default
    // (CLI picks · <cur_model>) 一行.
    const claudeTiers = new Set(["", "claude", "opus", "sonnet", "haiku"]);
    const isClaudeNative = claudeTiers.has(_tierFromModel(curM));
    document.querySelectorAll(
      '#model-menu .model-menu-item[data-model="opus"], '
      + '#model-menu .model-menu-item[data-model="sonnet"], '
      + '#model-menu .model-menu-item[data-model="haiku"]',
    ).forEach(b => { b.hidden = !isClaudeNative; });
    // chat-head model button icon 跟随当前选定 model: model 非空 → 用对应
    // tier 的 SVG; 空 (Default) → 用 default item 的 icon (即 cur_model
    // 推断出的 tier icon, 在 defIcon 同步后是最新). guard innerHTML 写入
    // 防止每秒无差别重写造成桌面端微闪 / SVG 重绘.
    const modelBtn = document.getElementById("chat-menu-model-btn");
    if (modelBtn) {
      const sel = model
        ? `.model-menu-item[data-model="${model}"] .model-menu-icon`
        : '.model-menu-item[data-model=""] .model-menu-icon';
      const src = document.querySelector(sel);
      if (src) {
        const want = src.innerHTML;
        if (modelBtn.dataset.iconCache !== want) {
          modelBtn.innerHTML = want;
          modelBtn.dataset.iconCache = want;
        }
      }
    }
  }
  // turn-card 跟随刷新 (icon / token / duration)
  _refreshTurnCard();
}

function _fmtTok(n) {
  // 千分位分隔的具体数字 + "t" 后缀. 无空格. 例: 1234 → "1,234t",
  // 1234567 → "1,234,567t". 用于单轮 output token 计数 (cs-tokens /
  // msg-tokens) — 精确到个位.
  return (n || 0).toLocaleString("en-US") + "t";
}

function _fmtCtx(n) {
  // ctx tooltip 用的简写格式: 1234 → "1.2k", 1234567 → "1.2M".
  // 数字和单位之间无空格. ctx total/limit 通常是十万 / 百万级, 简写更直观.
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n || 0);
}
// 每秒刷新一次（让 turn 计时器跑起来）
setInterval(refreshConvStatus, 1000);

// chat-menu 的 model / effort select 共用的 PATCH handler — 用户改任一
// 改 model 立即 PATCH. effort UI 已移除, 不发 effort 字段 → 后端
// ModelEffortRequest.effort=None → manager.update_model_effort 保留原值.
// server 端 kill 当前 proc, 下次 send 用新 args resume.
async function _patchSessionModelEffort() {
  if (!state.sessionId) return;
  const modelSel = document.getElementById("chat-menu-model");
  if (!modelSel) return;
  const body = JSON.stringify({ model: modelSel.value || "" });
  try {
    const r = await api(
      `/api/sessions/${encodeURIComponent(state.sessionId)}/model_effort`,
      { method: "PATCH", body },
    );
    const sess = state.sessionsById.get(state.sessionId);
    if (sess) {
      sess.model = r.model || "";
      sess.effort = r.effort || "";
    }
    refreshChatMeta();
  } catch (e) {
    console.warn("update model/effort failed", e);
  }
}

// §2: keep "active N ago" labels ticking even when no new session_state
// arrives. Updates only the .ts text node — no card-DOM rebuild — so we
// don't lose focus/scroll/animation state.
// Same ticker also adds/removes the .stalled class on state-busy cards
// when no new visible activity has arrived for STALLED_BUSY_THRESHOLD_S
// seconds — flips the green dot to yellow to surface "in flight but
// silent" sessions.
const STALLED_BUSY_THRESHOLD_S = 300;
setInterval(() => {
  const now = Date.now() / 1000;
  document.querySelectorAll(".session-card").forEach((card) => {
    const sid = card.dataset.id;
    if (!sid) return;
    const sess = state.sessionsById.get(sid);
    if (!sess) return;
    const tsEl = card.querySelector(".ts");
    if (tsEl) {
      const txt = relTime(sess.last_activity_at) + " ago";
      if (tsEl.textContent !== txt) tsEl.textContent = txt;
    }
    // .stalled toggle: busy AND silence > threshold
    const stalled = sess.state === "busy"
      && (now - (sess.last_activity_at || 0)) > STALLED_BUSY_THRESHOLD_S;
    card.classList.toggle("stalled", stalled);
  });
}, 250);

// 键盘 / chat-foot 位置: 交给浏览器 (含 iOS Safari) 自己处理. 之前一堆
// pinChatHead / syncKbInset / dvh / vp 写入策略都被实测证明会跟 iOS
// 自身的 auto-scroll 撞车制造新问题, 直接撤干净, 只信 CSS.

// chat-head 高度因设备 (Dynamic Island / notch / 桌面) 差异从 56 到
// 111+ px 都有可能. chat-log 的 padding-top 写死 56 + safe-area-inset
// 在 Dynamic Island 设备会盖不住 head, 部分消息藏在 head 后. 实测 head
// 高度写到 --chat-head-h, CSS 用这个变量做 padding-top.
function syncChatHeadHeight() {
  const head = document.getElementById("chat-head");
  if (!head) return;
  const r = head.getBoundingClientRect();
  const h = r.height;
  // head display:none 时 h=0 — 必须显式写 0px (而不是保留旧值), 否则
  // 键盘弹起 head 隐藏后 .chat 仍预留一截空白.
  document.documentElement.style.setProperty(
    "--chat-head-h", (h > 0 ? h : 0) + "px"
  );
  // 把每次实测尺寸打到后端日志, 方便诊断 "消息藏在 head 后"
  const log = document.getElementById("chat-log");
  const logR = log ? log.getBoundingClientRect() : null;
  const padTop = log ? parseFloat(getComputedStyle(log).paddingTop) : null;
  const headHVar = getComputedStyle(document.documentElement)
                     .getPropertyValue("--chat-head-h").trim();
  // 取 chat-log 第一个子在 viewport 的位置 — 判断它是否被 head 遮住
  const firstChild = log && log.firstElementChild;
  const fcR = firstChild ? firstChild.getBoundingClientRect() : null;
  try {
    dbgLog("head-sync", {
      ua: navigator.userAgent.slice(0, 80),
      isPwa: document.body.classList.contains("is-pwa"),
      hasSession: document.body.classList.contains("has-session"),
      headDisplay: getComputedStyle(head).display,
      head: { h: Math.round(h), y0: Math.round(r.top), y1: Math.round(r.bottom) },
      log: logR && { h: Math.round(logR.height),
                     y0: Math.round(logR.top), y1: Math.round(logR.bottom) },
      padTop: padTop != null ? Math.round(padTop * 10) / 10 : null,
      headHVar,
      firstChild: fcR && {
        tag: firstChild.tagName + "." + (firstChild.className || "").split(" ")[0],
        y0: Math.round(fcR.top), y1: Math.round(fcR.bottom),
        hiddenByHead: fcR.top < r.bottom - 1,
      },
      vp: window.visualViewport ? {
        h: Math.round(window.visualViewport.height),
        offTop: Math.round(window.visualViewport.offsetTop),
      } : null,
    });
  } catch (_) {}
}
if (typeof ResizeObserver !== "undefined") {
  const head = document.getElementById("chat-head");
  if (head) new ResizeObserver(syncChatHeadHeight).observe(head);
}
window.addEventListener("resize", syncChatHeadHeight);
// 多次重测兜底: 字体加载 / iOS safe-area-inset 第一次计算 / chat-head
// 从 translateX(100%) 滑入时, 不同时机的 bbox 可能不同, 多打几枪保险.
syncChatHeadHeight();
window.addEventListener("load", syncChatHeadHeight);
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(syncChatHeadHeight).catch(() => {});
}
[100, 300, 800, 1500].forEach(t => setTimeout(syncChatHeadHeight, t));

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
// chat-log 内容长大但 scrollTop 不变时, scroll 事件不会触发 sync, ↓ 按钮
// 不会按时出现. 用 MutationObserver 监听 subtree 变更和 ResizeObserver
// 监听容器尺寸变更 — 两个一起覆盖所有 "距底距离改变" 的场景:
//   - 新消息流入: childList 改变 → MutationObserver
//   - 图片 / markdown 渲染完毕高度突变: ResizeObserver
//   - 容器自身 resize (键盘 / 横竖屏): 已有 window resize 监听
(function setupScrollBtnAutoSync() {
  const log = document.getElementById("chat-log");
  if (!log) return;
  // 在 idle 时合批 — 避免每个子节点变更各触发一次
  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; syncScrollToBottomBtn(); });
  }
  new MutationObserver(schedule).observe(log, {
    childList: true, subtree: true, characterData: true,
  });
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(schedule).observe(log);
  }
})();

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

// 数 chat-log 里直接可见的"卡"数 — 这是用户视角的消息单位.
// .bubble (user/assistant/system) / .tool-group (合并的连续工具调用) /
// .perm-card / .askuser-card 各算 1 张. tool-card 在 tool-group 内不
// 单独计 (group 是单一可见卡). 用 :scope > ... 只数顶级子节点.
function countVisibleChatCards() {
  const log = document.getElementById("chat-log");
  if (!log) return 0;
  return log.querySelectorAll(
    ":scope > .bubble, :scope > .tool-group, " +
    ":scope > .perm-card, :scope > .askuser-card"
  ).length;
}

// 一次"加载更早"的可见卡目标 + 单批 raw event 数 + 硬上限.
// 可见卡 = .bubble / .tool-group / .perm-card / .askuser-card.
// PAGE_SIZE 是 server messages API 的 raw event 单位 — 一条 assistant
// message 在 db 里可能由 N 个 stream_event 行组成, 渲染后才合并成 1 卡,
// 所以 PAGE_SIZE 要明显 > TARGET 否则单批渲不出 TARGET 张卡.
// HARD_CAP 是退出兜底: 即使顶端仍是 .tool-group (理论上要继续拉求完整),
// visibleCards 一旦 ≥ HARD_CAP 也强制停 — 单次上拉不让飞到几十张.
const HISTORY_VISIBLE_TARGET = 20;
const HISTORY_VISIBLE_HARD_CAP = 30;
const HISTORY_PAGE_SIZE = 50;
const HISTORY_MAX_BATCHES = 5;
// 进入 chat 后, 静默续拉到 chat-log 的 scrollHeight ≥ 2 × viewport 高度.
// 不按"可见卡数"算 — 用户按实际内容铺满程度判: 2 屏内容看完, 用户主动
// 上拉到顶再加载更多.

function countVisibleInFragment(frag) {
  // DocumentFragment 上 querySelectorAll(":scope > X") 会返回 0 — :scope
  // 在 fragment 上不可用 (规范 / 浏览器实测都如此). 必须手动遍历 children.
  // 之前用 :scope > 写法, visibleCards 永远是 0, HARD_CAP 退出条件失效,
  // 单次上拉跑满 MAX_BATCHES 拉远超 target 的卡数 (实测 ~70 张).
  let n = 0;
  const kids = frag.children;
  for (let i = 0; i < kids.length; i++) {
    const cl = kids[i].classList;
    if (cl && (cl.contains("bubble") || cl.contains("tool-group")
               || cl.contains("perm-card") || cl.contains("askuser-card"))) {
      n++;
    }
  }
  return n;
}

// 静默续拉到 ≥ 20 张可见卡 (autoFillInitialCards) 共用同一份循环逻辑,
// 区别只在: silent=true 时不显示 #history-loader 转圈卡 — 用户进 chat
// 看到的就是渲完的内容, 不见"还在加载"提示.
async function loadEarlierHistory(opts) {
  opts = opts || {};
  const silent = !!opts.silent;
  if (!state.sessionId || !state.hasMoreHistory || state.loadingHistory) return;
  if (state.firstSeq == null) return;
  const log = $("chat-log");
  state.loadingHistory = true;
  state.suppressScrollLoad = true;
  if (!silent) setHistoryLoader("Loading earlier messages…");
  const savedBehavior = log.style.scrollBehavior;
  log.style.scrollBehavior = "auto";

  // workFrag 累积多批渲染结果. 顶端 = 最老, 末端 = 较新但仍比 chat-log
  // 现有内容更老. 渲染期间 chatRoot() 看 state.earlierFragment 重定向.
  const workFrag = document.createDocumentFragment();
  const savedToolGroup = state.currentToolGroup;
  let beforeSeq = state.firstSeq;
  let firstSeqAfter = state.firstSeq;
  let hasMore = true;
  let batches = 0;

  try {
    while (batches++ < HISTORY_MAX_BATCHES) {
      // 每批独立 fragment, 独立 currentToolGroup. 渲完跟 workFrag 合并
      // (跨批 tool-group 拼接) 然后 prepend.
      const batchFrag = document.createDocumentFragment();
      state.earlierFragment = batchFrag;
      state.currentToolGroup = null;

      let data, earlier;
      try {
        data = await api(
          `/api/sessions/${encodeURIComponent(state.sessionId)}/messages?` +
          `before_seq=${beforeSeq}&limit=${HISTORY_PAGE_SIZE}`
        );
        earlier = data.messages || [];
      } finally {
        state.earlierFragment = null;
      }

      if (earlier.length === 0) {
        hasMore = false;
        break;
      }

      // 渲染本批 (按 seq 升序) → batchFrag
      state.earlierFragment = batchFrag;
      try {
        for (const env of earlier) {
          try { handleEvent(env.event, env.ts); }
          catch (e) { console.warn("history render error", e); }
        }
      } finally {
        state.earlierFragment = null;
      }

      hasMore = !!data.has_more;
      firstSeqAfter = data.first_seq != null ? data.first_seq : earlier[0].seq;
      beforeSeq = firstSeqAfter;

      // 跨批 tool-group 合并: batchFrag 最末卡 + workFrag 顶端卡 若都
      // 是 .tool-group, 它们在时间上紧邻 (中间没有非 tool 消息切断),
      // 视觉应该是一个 group. 把 workFrag 顶端 group 的 tool-cards
      // 整段 append 到 batchFrag 末端 group 的 .tool-group-body, 然后
      // 删掉空壳, 再 refreshToolGroup 重算 count/summary.
      const batchLast = batchFrag.lastElementChild;
      const workFirst = workFrag.firstElementChild;
      if (batchLast && workFirst
          && batchLast.classList.contains("tool-group")
          && workFirst.classList.contains("tool-group")) {
        const batchBody = batchLast.querySelector(".tool-group-body");
        const workBody = workFirst.querySelector(".tool-group-body");
        if (batchBody && workBody) {
          while (workBody.firstChild) {
            batchBody.appendChild(workBody.firstChild);
          }
          workFirst.remove();
          try { refreshToolGroup(batchLast); } catch (_) {}
        }
      }

      // batch 整体 prepend 到 workFrag (顶部 = 最老)
      workFrag.insertBefore(batchFrag, workFrag.firstChild);

      // 退出判据 (按优先级):
      //   ① !hasMore — 拉到顶必停
      //   ② visibleCards ≥ HARD_CAP — 硬上限, 即使顶端是 tool-group 也停
      //      (避免追求完整边界时无限拉, 用户上拉一次别加载几十条)
      //   ③ visibleCards ≥ TARGET 且顶端不是 tool-group — 正常退出
      const visibleCards = countVisibleInFragment(workFrag);
      const topIsToolGroup = !!(
        workFrag.firstElementChild
        && workFrag.firstElementChild.classList.contains("tool-group")
      );
      if (!hasMore) break;
      if (visibleCards >= HISTORY_VISIBLE_HARD_CAP) break;
      if (visibleCards >= HISTORY_VISIBLE_TARGET && !topIsToolGroup) break;
    }

    // 一次性 prepend, scrollHeight 差值锚定 scrollTop. 视觉锚点不飞.
    const heightBefore = log.scrollHeight;
    const scrollBefore = log.scrollTop;
    log.insertBefore(workFrag, log.firstChild);
    void log.offsetHeight;
    const heightAfter = log.scrollHeight;
    log.scrollTop = scrollBefore + (heightAfter - heightBefore);

    state.firstSeq = firstSeqAfter;
    state.hasMoreHistory = hasMore;
  } catch (e) {
    console.warn("loadEarlierHistory failed", e);
    if (!silent) {
      setHistoryLoader("Load failed, pull to retry");
      setTimeout(() => setHistoryLoader(null), 1500);
    }
    state.earlierFragment = null;
    state.currentToolGroup = savedToolGroup;
    state.loadingHistory = false;
    log.style.scrollBehavior = savedBehavior;
    setTimeout(() => { state.suppressScrollLoad = false; }, 200);
    return;
  }
  state.currentToolGroup = savedToolGroup;
  if (!silent) setHistoryLoader(null);
  state.loadingHistory = false;
  log.style.scrollBehavior = savedBehavior;
  setTimeout(() => { state.suppressScrollLoad = false; }, 200);
}

// backlog 渲完后, 静默续拉到 chat-log scrollHeight ≥ 2 × viewport 高度或拉到顶.
// silent=true 透传给 loadEarlierHistory, 不显示 #history-loader. 用户感觉
// 一进 chat 就有完整内容, 不见"还在加载"提示.
//
// 退出条件 (任一满足): ① 已到 target; ② 拉到顶 (!hasMoreHistory);
// ③ safety 兜底 (10 轮硬上限).
//
// 注: **不以"本轮没新增可见卡"作为 break 条件**. 原因: loadEarlier 一次
// 内部循环可能拉 5 × PAGE_SIZE = 250 个 raw events, 这些 events 里全是
// stream_event delta (修既有 bubble) 而无 content_block_start (产新卡)
// 是可能的, 尤其是最近一轮 claude 长输出的 session. 此时 visibleCards 不
// 增但 hasMore 仍 true, 应该继续拉更老的, 不能在此 break — 否则用户看到
// 14 张卡停下, autoFill 提前 give up 是 bug.
async function autoFillInitialCards() {
  if (state.autoFilling) return;
  state.autoFilling = true;
  try {
    const log = $("chat-log");
    const targetH = window.innerHeight * 2;
    let safety = 0;
    while (log && log.scrollHeight < targetH
           && state.hasMoreHistory
           && safety < 10) {
      const prevFirstSeq = state.firstSeq;
      await loadEarlierHistory({ silent: true });
      if (state.firstSeq === prevFirstSeq) break;
      safety++;
    }
  } finally {
    state.autoFilling = false;
    // overlay fade-out 现在由 backlog_done handler 立即做, 这里不再操心.
    // 即使 autoFill 卡住或 session 很长, 用户也已经看到当前批 backlog 内容.
  }
}

// chat-log 滚到顶部附近时拉更早历史；wheel/touch 顶部继续上拉也触发（chat-log 不可滚时兜底）
$("chat-log").addEventListener("scroll", () => {
  // 维护 state.atBottom — 新消息到达时 chatScrollBottom 据此决定是否跟进
  const log = $("chat-log");
  const distFromBottom = log.scrollHeight - log.scrollTop - log.clientHeight;
  state.atBottom = distFromBottom < 40;
  syncScrollToBottomBtn();
  if (state.suppressScrollLoad) return;
  if (log.scrollTop < 100) loadEarlierHistory();
});
window.addEventListener("resize", syncScrollToBottomBtn);
window.addEventListener("orientationchange", syncScrollToBottomBtn);
// 窗口尺寸变化（横竖屏 / 桌面 resize）也刷一次 chat-meta，确保窄宽阈值切换时显示项重算
window.addEventListener("resize", refreshChatMeta);
window.addEventListener("orientationchange", refreshChatMeta);
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
  // 首次失败用很短的 backoff (100ms), 通常进 chat 时第一次 ws 连接 race
  // (旧 ws 没完全关 / hibernated session wake-up) 一次就好, 不该让用户等 1s.
  // 后续失败按指数翻倍, 30s 封顶 — 真断网时不让客户端疯狂重试.
  let backoff = 100;
  let timer = null;
  const isOwn = () => state.sessionId === ownSessionId;

  function start() {
    if (!isOwn()) return;
    if (state.ws
        && (state.ws.readyState === WebSocket.OPEN
            || state.ws.readyState === WebSocket.CONNECTING)) return;
    try { dbgLog("ws-connecting", {
      tEnter: Math.round(performance.now() - (state._enterChatT0 || 0)),
    }); } catch (_) {}
    const ws = new WebSocket(url);
    state.ws = ws;
    const isCurrent = () => state.ws === ws && isOwn();
    ws.addEventListener("open", () => {
      if (!isCurrent()) return;
      try { dbgLog("ws-open", {
        tEnter: Math.round(performance.now() - (state._enterChatT0 || 0)),
      }); } catch (_) {}
      setConnDot("connected", "Connected");
      backoff = 100;   // 重连一次成功 → 重置 backoff, 下次 race 又是 100ms 起
      // 这次连接前已经处理过的最大 seq 作为 dedupe 边界：重连/缓存命中时只跳 server 重发的旧 backlog
      // 冷启动 maxSeq=0 → 边界=0 → 不会误伤 earlier 批（earlier 的 seq 比 recent 小，但都 > 0）
      state.dedupeBoundary = state.maxSeq || 0;
      state.loadingHistory = false;
      state.pendingScrollToBottomOnBacklog = state.dedupeBoundary === 0;
      // outbox 重发: WS 断后未 ack 的 user_message 自动重新投递.
      // 一旦 server 回 user_input event 带 client_msg_id, handleEvent dequeue.
      _outboxResend(state.sessionId);
    });
    ws.addEventListener("close", () => {
      if (!isCurrent()) return;
      // backoff < 1s 时不闪 error UI — 这是首次 race 的快速重连, 用户感知不到
      if (backoff >= 1000) {
        const secs = Math.round(backoff / 1000);
        setConnDot("error", `Disconnected, reconnecting in ${secs}s`);
      } else {
        setConnDot("connecting", "Connecting");
      }
      if (timer) clearTimeout(timer);
      timer = setTimeout(start, backoff);
      backoff = Math.min(30000, backoff * 2);
    });
    ws.addEventListener("error", () => { if (isCurrent()) setConnDot("error", "Connection error"); });
    ws.addEventListener("message", (ev) => {
      if (!isCurrent()) return;
      try {
        const _env = JSON.parse(ev.data);
        if (typeof _env.seq === "number" && _env.seq > 0) {
          // dedupe 仅针对本次连接前已处理的 seq；本次连接收到的事件（含 backlog 的 earlier 批）都放行
          if (_env.seq <= (state.dedupeBoundary || 0)) return;
          if (_env.seq > (state.maxSeq || 0)) state.maxSeq = _env.seq;
        }
        // IDB write: 仅持久化 server 自己持久化的 envelope (有 seq>0).
        // transient stream_event / content_block delta 没 seq, 不写.
        // 写完后 trim 老条目 (LRU per session).
        if (typeof _env.seq === "number" && _env.seq > 0
            && _idbWriteKind(_env.event)) {
          const sid = state.sessionId;
          idbPutMessage(sid, _env);
          // throttled trim — 不每次都跑, 每 100 个 envelope trim 一次
          state._idbWriteCount = (state._idbWriteCount || 0) + 1;
          if (state._idbWriteCount % 100 === 0) {
            idbTrimSession(sid);
          }
        }
        handleEvent(_env.event, _env.ts);
      } catch (e) { console.warn("bad ws msg", e, ev.data); }
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

// 渲染目标：first_paint → backlog_done 期间 earlier 批量渲到屏外 fragment，
// 一次性 prepend 到 chat-log，避免 reveal 等所有历史；live 流量始终走 chat-log。
function chatRoot() {
  return state.earlierFragment || $("chat-log");
}
function chatScrollBottom() {
  if (state.earlierFragment) return;
  // 仅在用户已经在底部时才跟进, 否则不抢阅读位置 — 这是 §4 的契约.
  if (!state.atBottom) return;
  const log = $("chat-log");
  log.scrollTop = log.scrollHeight;
}

function appendBubble(kind, text) {
  state.currentToolGroup = null;   // 非 tool 内容打断 tool-group 序列
  const root = chatRoot();
  const el = document.createElement("div");
  el.className = "bubble " + kind;
  if (kind === "assistant") {
    // assistant 加一行 meta（仿 tool-card 风格）：✦ Claude · ↓N tok · 状态点
    el.innerHTML = `
      <div class="msg-meta">
        <span class="msg-icon">✦</span>
        <span class="msg-model"></span>
        <span class="msg-tokens"></span>
        <span class="msg-status pending"></span>
      </div>
      <div class="msg-body"></div>`;
    if (text) el.querySelector(".msg-body").textContent = text;
  } else {
    el.textContent = text;
  }
  root.appendChild(el);
  chatScrollBottom();
  return el;
}

// thinking 状态已经由底部 conv-status 的脉冲点承担，不再用浮动占位卡
function showThinkingPlaceholder() {}
function hideThinkingPlaceholder() {}
function scheduleHideThinkingPlaceholder() {}

function updateAssistantMeta(bubble, info) {
  if (!bubble || !bubble.classList.contains("assistant")) return;
  if (info.model != null) {
    const e = bubble.querySelector(".msg-model");
    if (e) e.textContent = info.model;
  }
  if (info.tokens != null) {
    const e = bubble.querySelector(".msg-tokens");
    if (e) e.textContent = info.tokens ? "↓" + _fmtTok(info.tokens) : "";
  }
  if (info.status) {
    const e = bubble.querySelector(".msg-status");
    if (e) e.className = "msg-status " + info.status;
  }
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
  // assistant bubble 内部有 meta 头 + body：只更新 body，保留 meta
  let body = bubble.querySelector(".msg-body");
  if (!body) {
    // 非 assistant 或老格式 bubble：直接渲染整个 bubble
    bubble.innerHTML = renderMarkdown(text);
    renderMathIn(bubble);
    addCopyButtonsTo(bubble);
    return;
  }
  body.innerHTML = renderMarkdown(text);
  renderMathIn(body);
  addCopyButtonsTo(body);
}

// Copy / Check icon (常显 icon-only 按钮). 两个重叠方块表示复制, 对勾表示已复制.
const COPY_ICON_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
const CHECK_ICON_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="5 12 10 17 19 8"/></svg>';

// 扫描容器内所有 <pre>, 包到 .code-block-wrap 加右上角 .copy-code-btn.
// 幂等: 已包过的跳过. 流式渲染场景 (renderMD 被多次调用) 每次重新扫即可.
function addCopyButtonsTo(root) {
  const pres = root.querySelectorAll("pre");
  for (const pre of pres) {
    if (pre.closest(".code-block-wrap")) continue;
    const wrap = document.createElement("div");
    wrap.className = "code-block-wrap";
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(pre);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-code-btn";
    btn.title = "Copy code";
    btn.setAttribute("aria-label", "Copy code");
    // 两个 SVG: 默认 copy 图标 (两个重叠方块), 复制后 check 图标
    btn.innerHTML = COPY_ICON_SVG;
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const codeEl = pre.querySelector("code") || pre;
      const text = codeEl.textContent || "";
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        try {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          ta.remove();
        } catch (_) {}
      }
      btn.innerHTML = CHECK_ICON_SVG;
      btn.classList.add("copied");
      setTimeout(() => {
        btn.innerHTML = COPY_ICON_SVG;
        btn.classList.remove("copied");
      }, 1500);
    });
    wrap.appendChild(btn);
  }
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

function getOrCreateToolGroup() {
  const root = chatRoot();
  // 优先看 DOM 真实末尾 — 如果末尾就是一个 tool-group, 直接复用.
  // 这样即使 state.currentToolGroup 因 loadEarlierHistory / restoreSessionCache
  // 的 DOM 重排错位指向中部某个 group, 新到的 tool_use 也能正确并入
  // 视觉上的"最后一组"而不是另起一个.
  //
  // turn 进行时, active .turn-card 被 MutationObserver 粘在 chat-log 末尾,
  // 所以判断"末尾 tool-group"时要跳过它. 否则连续 tool_use 各起新 group —
  // 用户在后台 / 前台跑任务都能看到工具卡不合并的 bug.
  let last = root.lastElementChild;
  while (last && last.classList && last.classList.contains("turn-card")) {
    last = last.previousElementSibling;
  }
  if (last && last.classList && last.classList.contains("tool-group")) {
    state.currentToolGroup = last;
    return last;
  }
  let group = state.currentToolGroup;
  // 兜底: group 仍属 root, 且后面只剩 turn-card (视觉上仍是末尾 group) — 复用.
  if (group && group.parentNode === root) {
    let n = group.nextElementSibling, ok = true;
    while (n) {
      if (!n.classList || !n.classList.contains("turn-card")) { ok = false; break; }
      n = n.nextElementSibling;
    }
    if (ok) return group;
  }
  group = document.createElement("div");
  group.className = "tool-group collapsed";
  group.innerHTML = `
    <div class="tool-group-head" role="button" tabindex="0">
      <span class="group-icon">⚒</span>
      <span class="group-count"></span>
      <span class="group-summary"></span>
      <span class="group-status pending"></span>
    </div>
    <div class="tool-group-body"></div>`;
  root.appendChild(group);
  state.currentToolGroup = group;
  const head = group.querySelector(".tool-group-head");
  const toggle = () => group.classList.toggle("collapsed");
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return group;
}

// [进行时, 完成时, 名词]：根据 group 状态选用，未结束用 -ing
const TOOL_VERBS = {
  Read:      ["reading",     "read",       "file"],
  Edit:      ["editing",     "edited",     "file"],
  Write:     ["writing",     "wrote",      "file"],
  Bash:      ["running",     "ran",        "command"],
  Glob:      ["matching",    "matched",    "pattern"],
  Grep:      ["searching",   "searched",   "pattern"],
  WebFetch:  ["fetching",    "fetched",    "URL"],
  WebSearch: ["searching",   "searched",   "query"],
  Task:      ["dispatching", "dispatched", "task"],
  TodoWrite: ["updating",    "updated",    "todo"],
};
function refreshToolGroup(group) {
  if (!group) return;
  const body = group.querySelector(".tool-group-body");
  const cards = body.querySelectorAll(".tool-card");
  const n = cards.length;
  // 按 (工具名, 状态) 分桶统计：done/error 用过去式，pending 用进行时
  const doneCnt = {}, pendingCnt = {};
  const order = [];
  let hasPending = false, hasError = false;
  cards.forEach(c => {
    const name = c.querySelector(".tool-name")?.textContent || "tool";
    const s = c.querySelector(".tool-status");
    const isPending = s && s.classList.contains("pending");
    const isError = s && s.classList.contains("error");
    if (!order.includes(name)) order.push(name);
    if (isPending) { pendingCnt[name] = (pendingCnt[name] || 0) + 1; hasPending = true; }
    else            { doneCnt[name]    = (doneCnt[name]    || 0) + 1; if (isError) hasError = true; }
  });
  const partFor = (name, c, ing) => {
    const v = TOOL_VERBS[name];
    if (v) return `${ing ? v[0] : v[1]} ${c} ${v[2]}${c > 1 ? "s" : ""}`;
    return c > 1 ? `${name} ×${c}` : name;
  };
  const labels = [];
  // 先列已完成（过去式），再列进行中（-ing）
  for (const name of order) if (doneCnt[name])    labels.push(partFor(name, doneCnt[name], false));
  for (const name of order) if (pendingCnt[name]) labels.push(partFor(name, pendingCnt[name], true));
  let text = labels.join(", ");
  if (text) text = text.charAt(0).toUpperCase() + text.slice(1);
  group.querySelector(".group-count").textContent = text;
  // 不论几个子工具，标题里都不显示具体文件名/命令（展开看每个 tool-card）
  const sumEl = group.querySelector(".group-summary");
  if (sumEl) sumEl.textContent = "";
  const gs = group.querySelector(".group-status");
  gs.className = "group-status " + (hasPending ? "pending" : hasError ? "error" : "done");
}

function ensureToolCard(toolUseId, name) {
  let entry = state.toolById.get(toolUseId);
  if (entry) return entry;
  const group = getOrCreateToolGroup();
  const body = group.querySelector(".tool-group-body");
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
  body.appendChild(card);
  chatScrollBottom();
  const headEl = card.querySelector(".tool-head");
  const toggle = () => {
    const willExpand = card.classList.contains("collapsed");
    card.classList.toggle("collapsed");
    if (willExpand) maybeFetchToolLazy(toolUseId);
  };
  headEl.addEventListener("click", toggle);
  headEl.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  entry = {
    card,
    toolUseId,
    name: name || "tool",
    partialInput: "",
    finalInput: null,
    argsEl: card.querySelector(".tool-args"),
    resultEl: card.querySelector(".tool-result"),
    statusEl: card.querySelector(".tool-status"),
    summaryEl: card.querySelector(".tool-summary"),
    lazyInput: false,
    lazyResult: false,
    fetching: false,
  };
  state.toolById.set(toolUseId, entry);
  entry.group = group;
  refreshToolGroup(group);
  return entry;
}

async function maybeFetchToolLazy(toolUseId) {
  const entry = state.toolById.get(toolUseId);
  if (!entry) return;
  if (!entry.lazyInput && !entry.lazyResult) return;
  if (entry.fetching) return;
  // 卡已经折回去了 → 停止轮询
  if (entry.card && entry.card.classList.contains("collapsed")) return;
  entry.fetching = true;
  // 第一次拉：放 spinner 占位; 之后轮询里 fetch 时 argsEl 已经有正在生
  // 长的 partial_input 内容, 不能再覆盖回 spinner — 否则每 500ms 一次
  // 内容→spinner→内容 切换就是肉眼可见的闪烁. 用 entry._initialFetched
  // 一次性 flag 锁定首次状态.
  if (!entry._initialFetched && entry.argsEl) {
    entry.argsEl.innerHTML = '<span class="tool-spinner"></span><span class="tool-lazy-hint">loading…</span>';
    if (entry.resultEl) { entry.resultEl.hidden = true; entry.resultEl.innerHTML = ""; }
    entry._initialFetched = true;
  }
  try {
    const sid = state.sessionId;
    const data = await api(`/api/sessions/${encodeURIComponent(sid)}/tool/${encodeURIComponent(toolUseId)}`);
    if (state.sessionId !== sid) return;
    // input：优先用解析好的 input；没有就尝试 partial_input；都不行先保 spinner
    let inputObj = data.input;
    if (!inputObj && data.partial_input) {
      try { inputObj = JSON.parse(data.partial_input); } catch (_) { inputObj = null; }
    }
    if (inputObj) {
      entry.finalInput = inputObj;
      renderToolArgs(entry);
    } else if (data.partial_input) {
      // partial JSON 还无法 parse：先把累积的原文显示出来（mono 已经是预格式化）
      if (entry.argsEl) {
        entry.argsEl.innerHTML = "";
        entry.argsEl.textContent = data.partial_input + "…";
      }
    }
    if (data.has_result) {
      entry.lazyResult = false;
      renderToolResultBody(entry, data.result, !!data.is_error);
    }
    if (data.completed) {
      entry.lazyInput = false;
      entry.lazyResult = false;
      if (entry.poller) { clearTimeout(entry.poller); entry.poller = null; }
    } else {
      // 工具仍在跑：500ms 后再拉一次（除非卡又被折回去）
      if (entry.poller) clearTimeout(entry.poller);
      entry.poller = setTimeout(() => {
        entry.poller = null;
        if (entry.card && !entry.card.classList.contains("collapsed")) {
          maybeFetchToolLazy(toolUseId);
        }
      }, 500);
    }
  } catch (e) {
    if (entry.argsEl) entry.argsEl.textContent = "Load failed: " + (e.message || e);
  } finally {
    entry.fetching = false;
  }
}

function renderToolResultBody(entry, content, isError) {
  let body;
  if (typeof content === "string") body = content;
  else if (Array.isArray(content)) {
    body = content.map(c => (c.type === "text" ? c.text : JSON.stringify(c))).join("\n");
  } else body = JSON.stringify(content);
  entry.resultEl.hidden = false;
  entry.resultEl.classList.toggle("error", !!isError);
  entry.resultEl.innerHTML = "";
  entry.resultEl.textContent = body;
  entry.statusEl.className = "tool-status " + (isError ? "error" : "done");
  if (entry.group) refreshToolGroup(entry.group);
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
    return n ? `${n} item${n > 1 ? "s" : ""}` : "";
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
  if (entry.group) refreshToolGroup(entry.group);
  // tool input 含 file_path 时, 在 tool-head 右侧加个 ⬇ 下载按钮.
  // Edit / Write / Read / MultiEdit / NotebookEdit 都覆盖.
  _ensureToolDownloadBtn(entry);
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

// 给 tool-head 加 ⬇ 下载按钮 — input.file_path 存在就加. 已加过的不重复.
// 点 button → fetch /api/sessions/<sid>/file?path=... → blob → 触发下载.
// 走 fetch + blob 是为了同时兼容 hub mode (cookies) + local mode (bearer).
function _ensureToolDownloadBtn(entry) {
  if (!entry || !entry.card) return;
  const inp = entry.finalInput;
  if (!inp || typeof inp !== "object") return;
  const fp = inp.file_path;
  if (!fp || typeof fp !== "string") return;
  const head = entry.card.querySelector(".tool-head");
  if (!head) return;
  if (head.querySelector(".tool-download")) return;   // 已加过
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "tool-download";
  const fname = fp.split("/").pop() || "file";
  btn.title = "下载 " + fname;
  btn.setAttribute("aria-label", "下载文件");
  btn.innerHTML =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    + '<polyline points="7 10 12 15 17 10"/>'
    + '<line x1="12" y1="15" x2="12" y2="3"/></svg>';
  btn.addEventListener("click", (e) => {
    e.stopPropagation();    // 防触发 head 的 collapse toggle
    _downloadSessionFile(fp, btn);
  });
  // 插在 .tool-status 之前 (右侧位置), 跟 status 圆点同行右对齐
  const status = head.querySelector(".tool-status");
  if (status) head.insertBefore(btn, status);
  else head.appendChild(btn);
}

async function _downloadSessionFile(filePath, btnEl) {
  const sid = state.sessionId;
  if (!sid) return;
  const url = "api/sessions/" + encodeURIComponent(sid)
    + "/file?path=" + encodeURIComponent(filePath);
  const headers = {};
  if (!state.hubMode && state.token) {
    headers["Authorization"] = "Bearer " + state.token;
  }
  if (btnEl) btnEl.classList.add("downloading");
  try {
    const res = await fetch(apiPath(url), {
      headers,
      credentials: state.hubMode ? "include" : "same-origin",
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        if (j && j.detail) detail = j.detail;
      } catch (_) {}
      alert("下载失败 (" + res.status + "): " + detail);
      return;
    }
    const blob = await res.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filePath.split("/").pop() || "file";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
  } catch (e) {
    alert("下载失败: " + (e.message || e));
  } finally {
    if (btnEl) btnEl.classList.remove("downloading");
  }
}

function renderEditDiff(entry) {
  const inp = entry.finalInput;
  entry.argsEl.innerHTML = "";
  const header = document.createElement("div");
  header.className = "diff-header";
  const path = String(inp.file_path);
  header.innerHTML = `<span class="diff-path">${escHTML(path)}</span>`;
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
  // backlog/分页推过来的占位：body 留空 + 标 lazy，展开时按需拉
  if (content && typeof content === "object" && !Array.isArray(content)
      && content.__ccr_lazy === true) {
    entry.lazyResult = true;
    entry.statusEl.className = "tool-status " + (isError ? "error" : "done");
    entry.statusEl.textContent = "";
    if (entry.group) refreshToolGroup(entry.group);
    // 卡片当时若已经被展开（用户在工具运行时点了打开），把刚才的 "running…" 占位刷成真内容
    if (entry.card && !entry.card.classList.contains("collapsed")) {
      maybeFetchToolLazy(entry.toolUseId);
    }
    return;
  }
  renderToolResultBody(entry, content, isError);
  entry.statusEl.textContent = "";   // 完成后只用一个色点表示状态
  // 不再自动改 collapsed：默认就收起，用户想看详情自行点击展开（出错用红点提示）
  chatScrollBottom();
  // 工具结果回喂给 claude → 下一波 message_start 之前 = 真正的"思考"时间
  showThinkingPlaceholder(state.currentMsgModel);
}

// 调试小钩子：调试模式下把任意 tag/data 打到服务端日志，方便手机端不能看 console 时排查
function dbgLog(tag, data) {
  try { console.log("[" + tag + "]", data); } catch (_) {}
  if (!state.token) return;
  fetch(apiPath("/api/dbg/log"), {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": "Bearer " + state.token },
    body: JSON.stringify({ tag, data }),
  }).catch(() => {});
}

// AskUser tool 别名集合: builtin "AskUserQuestion" + 我们自定义的 MCP
// "mcp__ccr__ask_user". 任何 tool_use.name 命中这两个之一都路由到 askuser-card,
// 不渲染普通 tool-card (避免 user 看到丑陋的 mcp__ccr__ask_user 名字).
const ASKUSER_TOOL_NAMES = new Set([
  "AskUserQuestion",
  "mcp__ccr__ask_user",
]);
function isAskUserTool(name) { return ASKUSER_TOOL_NAMES.has(name); }

// ---------- AskUserQuestion 交互卡 ----------
// 后端 PreToolUse hook 命中 AskUserQuestion 时挂起整条 hook 调用，把 `_ccr askuser_request`
// 推给前端。这里据此渲染交互卡。用户提交答案 → WS `askuser_answer` → 后端 resolve hook，
// 先给 claude stdin 写 tool_result 再 return allow。
// 局限（!）：claude CLI 在 `--print` 模式下会在 emit tool_use 之后**自己内部**合成一条
// is_error=true content="Answer questions?" 的 tool_result 写进它发往 Anthropic API 的
// 消息流。我们注入的真答案在它消息流里是第二条，模型大概率用第一条，结果就是 agent 看到
// "Answer questions?"。CCR 后端层面已经能拿到真答案（DB 里有），但 agent 用不上。
// 要真可用还需要 MCP 自定义工具或代理 Anthropic API（见 todo）。
function ensureAskUserCard(toolUseId) {
  let entry = state.askuserById.get(toolUseId);
  if (entry) return entry;
  const root = chatRoot();
  state.currentToolGroup = null;   // 跟普通工具卡一样：打断 group 序列
  const card = document.createElement("div");
  card.className = "askuser-card pending";
  card.dataset.toolUseId = toolUseId;
  card.innerHTML = `
    <div class="askuser-head">
      <span class="askuser-icon">❔</span>
      <span class="askuser-title">Question</span>
      <span class="askuser-status pending">Waiting for input…</span>
    </div>
    <div class="askuser-body"></div>
    <div class="askuser-foot" hidden>
      <button class="askuser-submit btn btn-primary" type="button">Submit</button>
    </div>`;
  root.appendChild(card);
  chatScrollBottom();
  entry = {
    toolUseId,
    card,
    bodyEl: card.querySelector(".askuser-body"),
    footEl: card.querySelector(".askuser-foot"),
    submitBtn: card.querySelector(".askuser-submit"),
    statusEl: card.querySelector(".askuser-status"),
    questions: null,      // 完整 input 到了再填
    answers: null,        // 每题一个：单选 string | 多选 string[]
    submitted: false,
  };
  state.askuserById.set(toolUseId, entry);
  entry.submitBtn.addEventListener("click", () => submitAskUser(toolUseId));
  return entry;
}

function populateAskUserCard(entry, input) {
  if (!entry || !input || !Array.isArray(input.questions)) return;
  if (entry.questions) return;   // 已渲染过，不重复
  entry.questions = input.questions;
  entry.answers = entry.questions.map(q => q.multiSelect ? [] : null);
  entry.statusEl.textContent = "";
  entry.statusEl.classList.remove("pending");
  entry.bodyEl.innerHTML = "";
  entry.questions.forEach((q, qi) => {
    const block = document.createElement("div");
    block.className = "askuser-question";
    const qText = document.createElement("div");
    qText.className = "askuser-q";
    qText.textContent = q.question || q.header || `Question ${qi + 1}`;
    block.appendChild(qText);
    if (q.header && q.header !== qText.textContent) {
      const sub = document.createElement("div");
      sub.className = "askuser-sub";
      sub.textContent = q.header;
      block.appendChild(sub);
    }
    const opts = document.createElement("div");
    opts.className = "askuser-options";
    (q.options || []).forEach(o => {
      // 用 div + role="button"：避开 iOS Safari 下 <button> + display:flex 的渲染/点击异常
      const btn = document.createElement("div");
      btn.className = "askuser-option";
      btn.setAttribute("role", "button");
      btn.setAttribute("tabindex", "0");
      btn.dataset.label = o.label;
      btn.dataset.qi = String(qi);
      btn.innerHTML = `
        <span class="askuser-option-label">${escHTML(o.label || "")}</span>
        ${o.description ? `<span class="askuser-option-desc">${escHTML(o.description)}</span>` : ""}`;
      const trigger = (e) => {
        e.preventDefault();
        toggleAskUserOption(entry, qi, o.label, btn);
      };
      btn.addEventListener("click", trigger);
      btn.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") trigger(e);
      });
      opts.appendChild(btn);
    });
    block.appendChild(opts);
    entry.bodyEl.appendChild(block);
  });
  entry.footEl.hidden = false;
  refreshAskUserSubmit(entry);
  chatScrollBottom();
}

function toggleAskUserOption(entry, qi, label, btn) {
  if (entry.submitted) return;
  const q = entry.questions[qi];
  if (q.multiSelect) {
    const arr = entry.answers[qi];
    const i = arr.indexOf(label);
    if (i >= 0) { arr.splice(i, 1); btn.classList.remove("selected"); }
    else        { arr.push(label);  btn.classList.add("selected"); }
  } else {
    entry.answers[qi] = label;
    // 同一问的其它选项取消高亮
    entry.bodyEl.querySelectorAll(`.askuser-option[data-qi="${qi}"]`).forEach(b => {
      b.classList.toggle("selected", b === btn);
    });
  }
  refreshAskUserSubmit(entry);
}

function refreshAskUserSubmit(entry) {
  // 必填校验：每题至少要选一个；多选可空 → 也允许（按 Claude Code 行为，空数组也算合法）
  const allAnswered = entry.questions.every((q, i) => {
    const a = entry.answers[i];
    if (q.multiSelect) return true;          // 多选允许 0 项
    return a !== null && a !== undefined;
  });
  entry.submitBtn.disabled = !allAnswered;
}

function submitAskUser(toolUseId) {
  const entry = state.askuserById.get(toolUseId);
  if (!entry || entry.submitted) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  // 跟 Claude Code CLI 一致的 tool_result 形态：每题一项 {"option": "<label>"}（多选时 option 是数组）
  const answers = entry.questions.map((q, i) => ({
    option: entry.answers[i],
  }));
  state.ws.send(JSON.stringify({
    type: "askuser_answer",
    tool_use_id: toolUseId,
    answers,
  }));
  entry.submitted = true;
  entry.submitBtn.disabled = true;
  entry.submitBtn.textContent = "Submitted";
  entry.card.classList.add("submitted");
  entry.card.classList.remove("pending");
  entry.statusEl.textContent = "Answered";
  // 选中的按钮锁住、其它的灰
  entry.bodyEl.querySelectorAll(".askuser-option").forEach(b => b.classList.add("disabled"));
}

function markAskUserAnswered(toolUseId) {
  // backlog/reload 路径：发现 tool_result 已有，把卡片标"已回答"
  const entry = state.askuserById.get(toolUseId);
  if (!entry || entry.submitted) return;
  entry.submitted = true;
  if (entry.submitBtn) {
    entry.submitBtn.disabled = true;
    entry.submitBtn.textContent = "Answered";
  }
  if (entry.bodyEl) {
    entry.bodyEl.querySelectorAll(".askuser-option").forEach(b => b.classList.add("disabled"));
  }
  entry.card.classList.add("submitted");
  entry.card.classList.remove("pending");
  if (entry.statusEl) entry.statusEl.textContent = "Answered";
}

// ---------- 权限请求卡片 ----------
function showPermissionRequest(evt) {
  const root = chatRoot();
  // 兜底：如果同一 req_id 卡片已经在 DOM 上了（比如 backlog 已显示，server 又重 push 一份），不重复创建
  // 注意：earlier 缓冲期间 fragment 和 chat-log 都要查
  if (document.querySelector(`.perm-card[data-req-id="${evt.req_id}"]`)) return;
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
      <span class="perm-status pending">Awaiting approval</span>
    </div>
    <div class="tool-args mono"></div>
    <div class="perm-actions">
      <button class="perm-btn allow"        data-decision="allow"  data-persist="">Allow once</button>
      <button class="perm-btn allow-tool"   data-decision="allow"  data-persist="tool">Always allow this tool</button>
      <button class="perm-btn allow-cmd"    data-decision="allow"  data-persist="command">Always allow this command</button>
      <button class="perm-btn deny"         data-decision="deny"   data-persist="">Deny</button>
    </div>
    <div class="perm-resolved" hidden></div>`;
  card.querySelector(".tool-args").textContent = argsText;
  card.querySelectorAll(".perm-btn").forEach(b => {
    b.addEventListener("click", () => sendDecision(evt.req_id, b.dataset.decision, b.dataset.persist));
  });
  root.appendChild(card);
  chatScrollBottom();
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
  const status = card.querySelector(".perm-status");
  const dec = evt.decision;
  let label, sCls, dotCls, prefix;
  if (dec === "allow")      { label = "Allowed"; sCls = "done";   dotCls = "allowed"; prefix = "✓ "; }
  else if (dec === "deny")  { label = "Denied";  sCls = "error";  dotCls = "denied";  prefix = "✗ "; }
  else                       { label = "Stale";   sCls = "stale";  dotCls = "stale";   prefix = "· "; }
  status.className = "perm-status " + sCls;
  status.textContent = label;
  // 禁用按钮 + 隐藏整个 actions 区（hidden 配 CSS [hidden] 规则才会真消失）
  card.querySelectorAll(".perm-btn").forEach(b => b.disabled = true);
  card.querySelector(".perm-actions").hidden = true;
  const resolved = card.querySelector(".perm-resolved");
  resolved.hidden = false;
  resolved.textContent = prefix + (evt.message || "");
  card.classList.add(dotCls);
  // 头部状态由 globalWS session_state 同步
}

// ---------- 事件分发 ----------
// 防御性 DOM ts-sort: server 端 envelope.seq + envelope.ts 已严格单调,
// WS / TCP 也保序, 正常不会乱. 但加一道兜底 —
//   1. 每次 handleEvent 进来记 envelope.ts.
//   2. handleEvent 完成后, 给 chat-log 新 append 的最末 child 打 data-ts.
//   3. 检测到 ts 比上一个事件早 → schedule microtask 做一次 DOM stable sort.
function _sortChatLogByTs() {
  const log = $("chat-log");
  if (!log) return;
  const items = Array.from(log.children);
  // 用 stable sort: 缺 data-ts 的保持原位置 — 给它一个微小的 epsilon
  // (用 DOM 原始 index 作 secondary key, sort by [ts, originalIndex]).
  const tagged = items.map((el, i) => {
    const v = el.dataset && el.dataset.ts;
    const t = v ? parseFloat(v) : NaN;
    return { el, t: Number.isFinite(t) ? t : null, i };
  });
  tagged.sort((a, b) => {
    if (a.t === null && b.t === null) return a.i - b.i;
    if (a.t === null) return a.i - b.i;
    if (b.t === null) return a.i - b.i;
    return (a.t - b.t) || (a.i - b.i);
  });
  // 重排 DOM (insertBefore 自动 reparent, 不 detach)
  for (let i = tagged.length - 1; i >= 0; i--) {
    log.insertBefore(tagged[i].el, log.children[i] || null);
  }
}
function handleEvent(evt, ts) {
  const t = evt && evt.type;
  if (!t) return;

  // 兜底乱序检测 — 仅对实时事件 (有 ts, 非历史回放).
  const _log = $("chat-log");
  const _childCountBefore = _log ? _log.children.length : 0;
  let _outOfOrder = false;
  if (ts && !state.isHistoryReplay) {
    if (state._lastEventTs != null && ts < state._lastEventTs - 1e-3) {
      _outOfOrder = true;
    }
    state._lastEventTs = Math.max(state._lastEventTs || 0, ts);
  }
  // 把实际 dispatch 推一帧之后再做 reorder, 但 dispatch 同步保留
  queueMicrotask(() => {
    if (_log && _log.children.length > _childCountBefore) {
      const newChild = _log.lastElementChild;
      if (newChild && !newChild.dataset.ts && ts) {
        newChild.dataset.ts = String(ts);
      }
    }
    if (_outOfOrder) _sortChatLogByTs();
  });


  if (t === "stream_event") return handleStreamEvent(evt.event || {});
  if (t === "assistant")    return handleAssistantMessage(evt.message || {});
  if (t === "user")         return handleUserMessage(evt.message || {});
  if (t === "user_input")   return handleUserInput(evt, ts);
  if (t === "system")       return handleSystem(evt);
  if (t === "result")       return handleResult(evt, ts);
  if (t === "_ccr") {
    if (evt.subtype === "first_paint") {
      try { dbgLog("first-paint", {
        tEnter: Math.round(performance.now() - (state._enterChatT0 || 0)),
      }); } catch (_) {}
      // recent 已经渲完；马上揭幕让用户看到最新消息，后续 earlier 渲到屏外 fragment
      const tst = evt.turn_state;
      if (tst && tst.turn_started_at) {
        state.turnStartAt = Math.round(tst.turn_started_at * 1000);
        state.turnEndAt = tst.turn_ended_at ? Math.round(tst.turn_ended_at * 1000) : null;
        if (typeof tst.output_tokens === "number") {
          state.curOutputTokens = tst.output_tokens;
          state.priorTurnOutput = tst.output_tokens;
        }
        if (tst.model) state.currentMsgModel = tst.model;
        if (typeof tst.input_tokens === "number") state.lastInputTokens = tst.input_tokens;
        if (typeof tst.input_total === "number") state.lastInputTotal = tst.input_total;
        const m = state.currentMsgModel || "";
        const oneM = /\[1m\]|-1m\b|opus-4-7|opus-4\.7|opus-4-8/i.test(m);
        state.contextLimit = oneM ? 1_000_000 : 200_000;
        if (state.lastInputTotal > state.contextLimit) state.contextLimit = 1_000_000;
        refreshChatMeta();
        // 显式 bootstrap 末轮 turn-card. _refreshTurnCard 期间 (replay)
        // 不自动建卡, 这里手动调一次: 已结束走 _renderTurnSummary 建 finalized,
        // 进行中走 _ensureTurnCard 建 active.
        if (tst.turn_ended_at) {
          _renderTurnSummary({
            turn_started_at: tst.turn_started_at,
            turn_ended_at: tst.turn_ended_at,
            output_tokens: tst.output_tokens || 0,
            model: tst.model || "",
          });
        } else {
          _ensureTurnCard();
        }
        refreshConvStatus();
      }
      hideThinkingPlaceholder();
      // 缓存命中：DOM 已经在屏上，只用 turn_state 刷一下，不重 reveal、不建缓冲、不切 currentToolGroup
      if (state.cacheHit) return;
      // 冷启动：揭幕 + 贴底；earlier 还在路上，spinner 保留到 backlog_done 才收
      const log0 = $("chat-log");
      setScrollTopInstant(log0, log0.scrollHeight);
      if (state.revealChat) state.revealChat();
      // 准备 earlier 缓冲：之后的 appendBubble / ensureToolCard / showPermissionRequest 都渲到 fragment 上
      state.earlierFragment = document.createDocumentFragment();
      state.currentToolGroup = null;   // earlier 区独立的 tool-group 链，不复用 recent 末尾的
      return;
    }
    if (evt.subtype === "backlog_done") {
      state.firstSeq = evt.first_seq;
      state.hasMoreHistory = !!evt.has_more;
      state.isHistoryReplay = false;   // 之后的 user_input / result 才是实时
      try { dbgLog("backlog-done", {
        tEnter: Math.round(performance.now() - (state._enterChatT0 || 0)),
        first_seq: evt.first_seq, has_more: evt.has_more,
        history_count: evt.history_count, cacheHit: state.cacheHit,
        visibleNow: (function(){
          const log = document.getElementById("chat-log");
          if (!log) return -1;
          return log.querySelectorAll(
            ":scope > .bubble, :scope > .tool-group, "
            + ":scope > .perm-card, :scope > .askuser-card"
          ).length;
        })(),
      }); } catch (_) {}
      // 缓存命中：DOM 已经显示，server 这一波 backlog 已被 maxSeq dedupe 掉，啥都不用做
      if (state.cacheHit) {
        state.cacheHit = false;
        hideThinkingPlaceholder();
        return;
      }
      // 把 earlier fragment 一次性 prepend 到 chat-log 顶；前向贴底保持
      const log = $("chat-log");
      if (state.earlierFragment) {
        log.insertBefore(state.earlierFragment, log.firstChild);
        state.earlierFragment = null;
      }
      state.currentToolGroup = null;
      hideThinkingPlaceholder();
      // backlog_done 立即 fade-out chat-loading overlay — 当前批 backlog
      // 已渲到 chat-log, 用户可以看到. autoFill 后台续拉更早历史让用户
      // 上滑顺畅, 但不再阻塞 reveal (之前等 autoFill 满 2 屏才 fade-out,
      // session 很长 / loadEarlier 卡时用户看到"一直转圈圈").
      const _ld = $("chat-loading");
      if (_ld && !_ld.hidden) {
        _ld.classList.add("fade-out");
        setTimeout(() => {
          _ld.hidden = true;
          _ld.classList.remove("fade-out");
        }, 220);
      }
      // 用户的视觉锚点已经在底部（recent），多次 RAF 持续贴底应对 layout 抖动
      const stickBottom = () => setScrollTopInstant(log, log.scrollHeight);
      stickBottom();
      if (state.revealChat) state.revealChat();   // 兜底：first_paint 没到也能揭幕
      let n = 0;
      const settle = () => {
        stickBottom();
        if (++n < 30) requestAnimationFrame(settle);
      };
      requestAnimationFrame(settle);
      // autoFill 在后台续拉更早历史 (用户上滑顺畅), 不再控制 spinner.
      autoFillInitialCards();
      // 同 key 重复卡去重 (applyTurnState live path + _renderTurnSummary
      // replay path 可能给同一 turn 各建一张, 跨 root 漏 dedupe).
      _assertNoDupTurnCards("backlog-done-before-dedupe");
      _dedupeTurnCardsByKey();
      _assertNoDupTurnCards("backlog-done-after-dedupe");
      // 兜底: session 已结束 (state 非 busy/running) 时, 清掉因为
      // turn_state(end) 缺失而残留的 .turn-active turn-card.
      _reconcileTurnCardsAfterBacklog();
      state.pendingScrollToBottomOnBacklog = false;
      return;
    }
    if (evt.subtype === "permission_request") return showPermissionRequest(evt);
    if (evt.subtype === "permission_resolved") return markPermissionResolved(evt);
    if (evt.subtype === "permission_mode") return applyPermissionMode(evt.mode);
    if (evt.subtype === "turn_state") return applyTurnState(evt);
    if (evt.subtype === "turn_summary") return _renderTurnSummary(evt);
    if (evt.subtype === "askuser_request") {
      // hook-hold 流程：服务端在 PreToolUse hook 阶段就把这个事件推过来；
      // 它带完整 input（questions/options），我们立刻渲染卡片，等用户提交
      const ent = ensureAskUserCard(evt.tool_use_id);
      populateAskUserCard(ent, evt.tool_input || {});
      return;
    }
    if (evt.subtype === "askuser_resolved") {
      // 超时/取消：服务端标记此 askuser 不再可答
      if (evt.tool_use_id && evt.cancelled) markAskUserAnswered(evt.tool_use_id);
      return;
    }
    return;
  }
  if (t === "_internal") {
    // exit 状态由 session_state 同步（finished / exited），不再单独显示退出码
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
    }
    // 模型调用开始：先占位一个"思考中"卡片；首个 content_block_start 或 message_stop 时移除
    showThinkingPlaceholder((ev.message && ev.message.model) || state.currentMsgModel);
    // 拿到本轮 prompt 的 input_tokens（确定值）；output_tokens 跨 message 累加，权限等中断后续 message 不重置
    const u = (ev.message && ev.message.usage) || {};
    state.lastInputTokens = u.input_tokens || 0;   // fresh 上行（不含 cache_read）
    state.lastInputTotal = (u.input_tokens || 0)
                         + (u.cache_read_input_tokens || 0)
                         + (u.cache_creation_input_tokens || 0);   // 完整 prompt 大小，用于 ctx%
    state.currentMsgModel = (ev.message && ev.message.model) || "claude";
    // 同步 contextLimit：Opus 4.7 / [1m] 后缀都是 1M context；常规模型 200K
    // 兜底：如果 prompt 已经 > 200K，那肯定不是 200K limit
    if (state.currentMsgModel) {
      const m = state.currentMsgModel;
      const oneM = /\[1m\]|-1m\b|opus-4-7|opus-4\.7|opus-4-8/i.test(m);
      state.contextLimit = oneM ? 1_000_000 : 200_000;
    }
    if (state.lastInputTotal > state.contextLimit) state.contextLimit = 1_000_000;
    // 本 message 字符数清零，从 priorTurnOutput 起算
    state.streamingChars = 0;
    state.curMsgOutputTokens = u.output_tokens || 0;
    state.curOutputTokens = (state.priorTurnOutput || 0) + (u.output_tokens || 0);
    refreshChatMeta();
    return;
  }
  if (sub === "message_delta") {
    const u = ev.usage || {};
    const d = ev.delta || {};
    if (typeof u.output_tokens === "number") {
      state.curMsgOutputTokens = u.output_tokens;
      state.curOutputTokens = (state.priorTurnOutput || 0) + u.output_tokens;
      const cur = state.activeMsgId && state.msgById.get(state.activeMsgId);
      if (cur && cur.bubble) updateAssistantMeta(cur.bubble, { tokens: u.output_tokens });
    }
    if (typeof u.input_tokens === "number") {
      state.lastInputTokens = u.input_tokens;
      state.lastInputTotal = u.input_tokens
                           + (u.cache_read_input_tokens || 0)
                           + (u.cache_creation_input_tokens || 0);
    }
    refreshChatMeta();
    return;
  }
  if (sub === "message_stop") {
    hideThinkingPlaceholder();   // 兜底（极端情况 message_start 后没有 content_block）
    // 把当前 message 的 output 滚进 priorTurnOutput，给下一条 message_start 当起点
    state.priorTurnOutput = state.curOutputTokens || state.priorTurnOutput || 0;
    // 当前 assistant bubble 状态点变成 done
    const cur = state.activeMsgId && state.msgById.get(state.activeMsgId);
    if (cur && cur.bubble) updateAssistantMeta(cur.bubble, { status: "done" });
    state.activeMsgId = null;
    return;
  }
  if (sub === "content_block_start") {
    const idx = ev.index;
    const cb = ev.content_block || {};
    // tool_use 的 input 还在流式生成（input_json_delta），仍算"思考"过程
    //   thinking-card 保留到 content_block_stop（input 完整）才 hide
    // text 块开始：立刻 hide（调试时间差用；恢复用 scheduleHideThinkingPlaceholder(10000)）
    if (cb.type === "text") hideThinkingPlaceholder();
    if (cb.type === "text") {
      if (state.activeMsgId == null) return;
      const bubble = appendBubble("assistant", "");
      // 默认 meta 隐藏；如果本 message 决定要调工具（stop_reason=tool_use），message_delta 时会显示
      updateAssistantMeta(bubble, { model: state.currentMsgModel || "claude", tokens: state.curMsgOutputTokens || 0, status: "pending" });
      state.msgById.set(state.activeMsgId, { bubble, text: "" });
      state.blocksByIdx.set(idx, { type: "text", msgId: state.activeMsgId });
    } else if (cb.type === "tool_use") {
      if (isAskUserTool(cb.name)) {
        // 交互卡：起骨架，input 完整后由 assistant 事件填问题/选项
        ensureAskUserCard(cb.id);
        state.blocksByIdx.set(idx, { type: "askuser", toolUseId: cb.id });
        hideThinkingPlaceholder();
        return;
      }
      const entry = ensureToolCard(cb.id, cb.name);
      const inp = cb.input || {};
      if (inp.__ccr_lazy === true) {
        // 服务端瘦身后的占位：标 lazy，summary 后续会被 assistant 事件刷
        entry.lazyInput = true;
        if (inp.__ccr_summary && entry.summaryEl) entry.summaryEl.textContent = inp.__ccr_summary;
      } else if (Object.keys(inp).length) {
        entry.finalInput = inp;
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
        // 单条 message 的 token 估算（流式时显示，message_delta 末次会用真实值覆盖）
        updateAssistantMeta(msg.bubble, { tokens: Math.round(msg.text.length / 2.8) });
        chatScrollBottom();
      }
    } else if (d.type === "input_json_delta" && block.type === "tool_use") {
      // 工具参数开始 stream → 工具调用真正开始，thinking-card 让位
      hideThinkingPlaceholder();
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
    // 实时 token 估算（Claude CLI 风格）：按 partial 文本字符数 / 2.8 估，最终 message_delta 真实值覆盖
    const piece = d.text || d.partial_json || "";
    if (piece) {
      state.streamingChars = (state.streamingChars || 0) + piece.length;
      state.curOutputTokens = (state.priorTurnOutput || 0) + Math.round(state.streamingChars / 2.8);
      refreshChatMeta();
    }
    return;
  }
  if (sub === "content_block_stop") {
    const block = state.blocksByIdx.get(ev.index);
    if (block && block.type === "tool_use") hideThinkingPlaceholder();
    // text 块 stop 时若 bubble 文本仍是空, 移除这张空白卡 — content_block_start
    // 会乐观创建 bubble 等 delta, 实测有时一段 text 块 0 内容 (e.g. 紧跟在
    // tool_use 后的结尾换行), 不删会留个空 bubble 在 tool-group 下方.
    if (block && block.type === "text") {
      const cur = block.msgId && state.msgById.get(block.msgId);
      if (cur && cur.bubble && (!cur.text || !cur.text.trim())) {
        try { cur.bubble.remove(); } catch (_) {}
        state.msgById.delete(block.msgId);
      }
    }
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
      if (isAskUserTool(b.name)) {
        // 最终 input 到位，渲染问题/选项；live 路径上 ensureAskUserCard 已经在 content_block_start 时建好
        const ent = ensureAskUserCard(b.id);
        populateAskUserCard(ent, b.input || {});
        continue;
      }
      const entry = ensureToolCard(b.id, b.name);
      const inp = b.input || {};
      if (inp.__ccr_lazy === true) {
        // backlog/分页推过来的占位：先只填头部 summary，body 等点击展开再拉
        entry.lazyInput = true;
        if (entry.summaryEl) entry.summaryEl.textContent = inp.__ccr_summary || "";
      } else {
        entry.lazyInput = false;
        entry.finalInput = inp;
        renderToolArgs(entry);
      }
    }
  }
}

function handleUserMessage(msg) {
  const cs = msg.content || [];
  for (const c of cs) {
    if (c.type === "tool_result" && c.tool_use_id) {
      // AskUserQuestion 的回答（包括本会话之前的 backlog 里的）→ 把交互卡标已回答
      if (state.askuserById.has(c.tool_use_id)) {
        markAskUserAnswered(c.tool_use_id);
        continue;
      }
      attachToolResult(c.tool_use_id, c.content, c.is_error);
    }
  }
}

function applyTurnState(evt) {
  // 后端是 turn 起止时间的唯一源；前端只是把秒级 ts 转成 ms 缓存起来供 cs-time 显示
  const prevEndAt = state.turnEndAt;
  state.turnStartAt = evt.turn_started_at ? Math.round(evt.turn_started_at * 1000) : null;
  state.turnEndAt   = evt.turn_ended_at   ? Math.round(evt.turn_ended_at   * 1000) : null;
  // 从"进行中"→"已结束"的活时切换：保留显示，让用户看到刚跑完的 token/time
  if (!prevEndAt && state.turnEndAt && state.turnStartAt) state.turnFresh = true;
  if (typeof evt.output_tokens === "number") state.curOutputTokens = evt.output_tokens;
  if (evt.model) state.currentMsgModel = evt.model;
  if (typeof evt.input_tokens === "number") state.lastInputTokens = evt.input_tokens;
  if (typeof evt.input_total === "number") state.lastInputTotal = evt.input_total;
  // Turn card 生命周期: 进行中 (started_at 有, ended_at 无) → ensure;
  // 结束边沿 (prev no end → now has end) → finalize.
  // ★ replay 期间 (isHistoryReplay=true) 不通过这里建卡 — 历史 turn 由
  // turn_summary 走 _renderTurnSummary 进 earlierFragment, 最后 turn 的
  // 卡由 first_paint snapshot 走 _refreshTurnCard 在 chat-log 建好.
  // 否则 applyTurnState 会为每个老 turn 在 chat-log 末尾再造一张卡,
  // 跟 turn_summary 的卡叠在一起出现"末尾连续两张相同 token-card".
  if (state.turnStartAt && !state.turnEndAt && !state.isHistoryReplay) {
    _ensureTurnCard();
  }
  if (!prevEndAt && state.turnEndAt) {
    _finalizeTurnCard();
  }
  refreshChatMeta();
  refreshConvStatus();
}

function handleUserInput(evt, envTs) {
  // outbox ack: server 把 client_msg_id 透传回来, 此刻消息确认已被
  // session_manager 持久化 + DB inject_event 完成 → 安全删 outbox.
  // 兼容老 server (没透传 client_msg_id): 实时路径下也按 FIFO 把最老
  // 一条 outbox 项删了 — 每收到一个 user_input echo 就删一项, 顺序对齐.
  if (evt && evt.client_msg_id) {
    idbDeleteOutbox(evt.client_msg_id);
  } else if (!state.isHistoryReplay && !state.earlierFragment
              && state.sessionId) {
    idbListOutboxBySess(state.sessionId).then(rows => {
      if (rows.length) idbDeleteOutbox(rows[0].client_msg_id);
    });
  }
  if (!state.isHistoryReplay && !state.earlierFragment) {
    // token / timer 真源在后端: _update_turn_state 判过 "前一轮是否真的
    // 结束", 已经把该清的清了, turn_state 广播会让 applyTurnState 镜像
    // 过来. 这里只重置纯前端累加器 + 标记 turnFresh.
    // earlierFragment guard: loadEarlierHistory 期间历史 user_input 渲到
    // 屏外 fragment, 不是实时新轮, 别误建 active turn-card.
    state.priorTurnOutput = 0;
    state.turnFresh = true;
    _ensureTurnCard();
  }
  refreshChatMeta();
  refreshConvStatus();
  const c = evt.content;
  if (typeof c === "string") {
    appendBubble("user", c);
    // 用户消息上屏后，claude 即将思考第一波 → 显示占位
    showThinkingPlaceholder(state.currentMsgModel || "Claude");
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
  showThinkingPlaceholder(state.currentMsgModel || "Claude");
}

function handleSystem(evt) {
  // init 时拿到 model id，按 model 调整上下文上限（Opus 4.7 1M / 其它 200k）
  if (evt.subtype === "init" && typeof evt.model === "string") {
    state.contextLimit = /\[1m\]|-1m$|opus-4-7\[1m\]/i.test(evt.model) ? 1_000_000 : 200_000;
    refreshChatMeta();
  }
}

function handleResult(evt, envTs) {
  // 状态由 globalWS session_state 同步；这里只更新 token / cost 显示
  if (typeof evt.total_cost_usd === "number") state.totalCostUsd = (state.totalCostUsd || 0) + evt.total_cost_usd;
  // result.usage 是整轮所有 message 的聚合；prompt size 已在 message_start / message_delta 更新
  // turn 结束时间走后端 turn_state 事件，这里不动 turnEndAt
  refreshChatMeta();
  refreshConvStatus();
  // 不在这里发通知 — 退回 home 后这段 WS 关了, 收不到 result. turn-end 通知
  // 通过 globalWS 的 session_state 边沿 (busy → !busy) 触发, 见 maybeNotify().
}

// Turn-end Web Notification — 用户在 chat-menu 开了 toggle, 浏览器授权,
// 页面不可见 (用户切走 / 锁屏) → 任意 session 的 busy → !busy 状态变化时
// 弹通知. tag = sid 让同 session 多次替换旧通知. 点通知 → focus 窗口 + 跳进
// 那个 session.
function maybeNotifyTurnEnd(sess) {
  try {
    if (typeof Notification === "undefined") return;
    if (Notification.permission !== "granted") return;
    if (!state.notifyOnTurnEnd) return;
    if (!sess || !sess.id) return;
    // 抑制规则: 仅当"标签页可见 且 窗口有焦点"时才不打扰 (用户真的在看).
    // 切到别的 tab / 浏览器失焦 / 切到别的 app / 锁屏 → 都弹.
    const visible = document.visibilityState === "visible";
    const focused = (typeof document.hasFocus === "function")
                      ? document.hasFocus() : true;
    if (visible && focused) return;
    const name = sess.name || "session";
    const sid = sess.id;
    const n = new Notification(`Turn finished — ${name}`, {
      body: "Click to return to ClaudeCodeRemote",
      tag: sid,
      icon: new URL("icon.svg", document.baseURI).pathname,
    });
    n.onclick = () => {
      try { window.focus(); } catch (_) {}
      const s = state.sessionsById.get(sid);
      if (s) {
        try { enterChat(s.id, s.name, s.cwd, s.state); } catch (_) {}
      }
      n.close();
    };
  } catch (e) {
    console.warn("notify failed:", e);
  }
}

function _currentSessionMeta() {
  // 当前 chat 的 session 元信息 (model/effort) — 从 home 列表的 sessionsById
  // 拿. 用户在 spawn modal 选定的值, server 端 normalize 后回传.
  const sid = state.sessionId;
  if (!sid) return { model: "", effort: "" };
  const s = state.sessionsById.get(sid);
  return {
    model: (s && s.model) || "",
    effort: (s && s.effort) || "",
  };
}

function refreshChatMeta() {
  const meta = $("chat-meta");
  if (!meta) return;
  // chat-head 的副标题: cwd · model · effort (后两段空就不显示). 视觉上紧凑一行
  // 让用户在 chat 顶部直接看到当前会话用的模型 / effort 等级.
  //
  // cwd 段独立 .meta-cwd 元素 (dir=rtl + 内嵌 <bdo dir=ltr>) — 空间不足时
  // 从左截断, 保留右侧 basename (优先显示最右名). model/effort 段不截.
  const cwd = state.cwdShort || "";
  const { model, effort } = _currentSessionMeta();
  meta.replaceChildren();
  if (cwd) {
    const cwdEl = document.createElement("span");
    cwdEl.className = "meta-cwd";
    cwdEl.setAttribute("dir", "rtl");
    const bdo = document.createElement("bdo");
    bdo.setAttribute("dir", "ltr");
    bdo.textContent = cwd;
    cwdEl.appendChild(bdo);
    cwdEl.title = cwd;
    meta.appendChild(cwdEl);
  }
  const tailParts = [];
  if (model) tailParts.push(model);
  if (effort) tailParts.push(effort);
  if (tailParts.length) {
    const tailEl = document.createElement("span");
    tailEl.className = "meta-tail";
    tailEl.textContent = (cwd ? " · " : "") + tailParts.join(" · ");
    meta.appendChild(tailEl);
  }
  // hub mode: chat-head 右侧 #chat-app-chip 显示当前 session 归属的 app.
  // 数据从 state.sessionsById (HTTP /api/sessions 拉的, 含 app_name/app_online).
  const chipEl = $("chat-app-chip");
  if (chipEl) {
    if (state.hubMode && state.sessionId) {
      const sess = state.sessionsById.get(state.sessionId);
      const aname = sess && sess.app_name;
      if (aname) {
        chipEl.textContent = aname;
        chipEl.title = aname + (sess.app_online ? "" : " (offline)");
        chipEl.classList.toggle("offline", sess.app_online === false);
        chipEl.hidden = false;
      } else {
        chipEl.hidden = true;
      }
    } else {
      chipEl.hidden = true;
    }
  }
  refreshConvStatus();   // 任意时刻 chat-meta 刷新都同步底部状态栏，下行 token 跟着 text_delta 实时
}

async function _waitWsOpen(timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return true;
    await new Promise(r => setTimeout(r, 40));
  }
  return false;
}

async function _spawnFromTmpAndSend(content, textForName) {
  const tmpSid = state.sessionId;
  // 真 spawn — cwd = ~, name="" (后端 normalize 成 "untitled"), 默认 perm
  let r;
  try {
    // 默认权限读 settings 里 ccr.defaultPermMode, 没设过用 manual.
    // 跟正式 new-session modal 的 _setSpawnPermMode 行为一致.
    const defaultPerm =
      localStorage.getItem("ccr.defaultPermMode") || "manual";
    r = await api("/api/spawn", {
      method: "POST",
      body: JSON.stringify({
        cwd: "~", name: "", permission_mode: defaultPerm,
        model: "", effort: "",
      }),
    });
  } catch (e) {
    alert("Failed to start session: " + (e.message || e));
    return;
  }
  // tmp session 不再需要 — 真 session 会通过 globalWS snapshot 推过来
  state.sessionsById.delete(tmpSid);
  // 临时把当前 sessionId 清掉, 避免 enterChat 内 saveCurrentSessionCache 误存 tmp
  state.sessionId = null;
  await enterChat(r.id, r.name, r.cwd);
  // 等 ws open (cache-miss 路径下 connectWS 已被调用)
  const ok = await _waitWsOpen(8000);
  if (!ok) {
    alert("Connection timeout while starting session");
    return;
  }
  state.ws.send(JSON.stringify({ type: "user_message", content }));
  // 自动命名: 消息文本前 30 字符, 去掉多余空格. 空就保留后端 "untitled".
  const cleanName = (textForName || "")
    .replace(/\s+/g, " ").trim().slice(0, 30);
  if (cleanName) {
    api(`/api/sessions/${encodeURIComponent(r.id)}/rename`, {
      method: "PUT",
      body: JSON.stringify({ name: cleanName }),
    }).catch(() => {});
  }
}

function sendUserMessage() {
  const ta = $("chat-input");
  const text = ta.value.trim();
  // tmp session 走特殊路径: 先 spawn + rename + send. 离 fn 立即返回.
  if (state.sessionId && state.sessionId.startsWith("tmp-")) {
    if (!text && state.attachments.length === 0) return;
    const images = state.attachments.filter(a => a.kind === "image");
    const files = state.attachments.filter(a => a.kind === "file" && a.path);
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
    ta.value = "";
    ta.style.height = "auto";
    // 发出去后清掉该 session 的草稿
    if (state.sessionId && !state.sessionId.startsWith("tmp-")) {
      localStorage.removeItem("ccr.draft." + state.sessionId);
    }
    const attCopy = state.attachments;
    state.attachments = [];
    renderAttachmentBar();
    _spawnFromTmpAndSend(content, text);
    return;
  }
  if ((!text && state.attachments.length === 0)
      || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  if (state.attachments.some(a => a.uploading)) {
    alert("Upload in progress, please wait");
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
  // outbox: 写一条 pending entry, 带 client_msg_id. server 回 user_input
  // event 时带回 client_msg_id, handleEvent 内 dequeue. WS 断 / reconnect
  // 时未 ack 的会被 _outboxResend 重发.
  const clientMsgId = _uuid();
  const sessId = state.sessionId;
  idbPutOutbox({
    client_msg_id: clientMsgId,
    sess_id: sessId,
    content,
    created_at: Date.now() / 1000,
    attempts: 1,
  });
  state.ws.send(JSON.stringify({
    type: "user_message", content, client_msg_id: clientMsgId,
  }));
  // user bubble 等 server 注入 user_input echo 时再渲染（保证刷新/resume 也能看到）
  ta.value = "";
  ta.style.height = "auto";
  // 发出去后清该 session 草稿
  if (state.sessionId && !state.sessionId.startsWith("tmp-")) {
    localStorage.removeItem("ccr.draft." + state.sessionId);
  }
  clearAttachments();
}

// WS open / reconnect 时调 — 扫该 sess 的 pending outbox, 重发.
// 先 purge stale: created > 30s 的视为 server 已收下但回送的 client_msg_id
// 字段被老 ws.py 丢了 (无法 ack). 直接放弃, 不再 retry 防止重复发送.
async function _outboxResend(sessId) {
  if (!sessId || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  await _outboxPurgeStale(sessId, 30);
  const pending = await idbListOutboxBySess(sessId);
  for (const e of pending) {
    try {
      state.ws.send(JSON.stringify({
        type: "user_message",
        content: e.content,
        client_msg_id: e.client_msg_id,
      }));
      // bump attempts (best effort, 不 await)
      idbPutOutbox({ ...e, attempts: (e.attempts || 1) + 1 });
    } catch (err) {
      console.warn("outbox resend failed:", err);
    }
  }
}

async function _outboxPurgeStale(sessId, maxAgeSec) {
  if (!sessId) return;
  const now = Date.now() / 1000;
  const rows = await idbListOutboxBySess(sessId);
  for (const e of rows) {
    if (now - (e.created_at || 0) > (maxAgeSec || 30)) {
      idbDeleteOutbox(e.client_msg_id);
    }
  }
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
      s.textContent = (a.uploading ? "⏳ " : "📎 ") + (a.name || a.label || "(attachment)");
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
  if (!state.sessionId) { alert("Open a session first"); return; }
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
    alert(`Upload failed: ${e.message}`);
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
  // 草稿持久化: 每 session 独立, 走 localStorage. 刷新 / 切 session 都保留.
  // tmp 前缀 session 不存 (没真 spawn 过, 跟着销毁).
  if (state.sessionId && !state.sessionId.startsWith("tmp-")) {
    const key = "ccr.draft." + state.sessionId;
    const v = e.target.value || "";
    if (v) localStorage.setItem(key, v);
    else localStorage.removeItem(key);
  }
});

// 切 session 时还原对应草稿 (在 enterChat 末尾或 reveal 时调).
function restoreChatInputDraft(sid) {
  const ta = $("chat-input");
  if (!ta) return;
  const draft = sid && !sid.startsWith("tmp-")
    ? (localStorage.getItem("ccr.draft." + sid) || "")
    : "";
  ta.value = draft;
  // 重算高度
  ta.style.height = "auto";
  ta.style.height = Math.min(160, ta.scrollHeight) + "px";
}

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
document.querySelectorAll("#theme-toggle").forEach(b => {
  b.addEventListener("click", toggleTheme);
});
// PWA service worker（只在 secure context 下有效；http 公网会静默失败，不影响功能）
// register 路径基于 <base href>，scope 同前缀
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    const swURL = new URL("sw.js", document.baseURI).pathname;
    const swScope = new URL("./", document.baseURI).pathname;
    // updateViaCache: 'none' — sw.js 永远不进 HTTP cache, 每次注册都从网络
    // 重新拉, 这样 bump CACHE 后用户下次访问就能装上新 SW (而不是默认
    // 24h 才主动检查更新). 注册后立即 update() 再保险一次.
    navigator.serviceWorker
      .register(swURL, { scope: swScope, updateViaCache: "none" })
      .then(reg => reg.update().catch(() => {}))
      .catch(err => console.warn("SW register failed (expected on http):", err.message));
  });
}
// M-Hub-4: probe /api/me 决定 hub mode + login flow. local mode 老路径不变.
(async function boot() {
  // 左边缘 transparent shield — 拦住 iOS PWA 系统右滑, 同时把 swipe-back
  // 手势转给当前 active overlay view (chat/settings/apps/help). home 上没
  // overlay = silent (吃手势但视觉不响应, 防"右滑出空白页 + reload" bug).
  try {
    const shield = document.createElement("div");
    shield.id = "ccr-edge-shield";
    shield.setAttribute("aria-hidden", "true");
    document.body.appendChild(shield);

    const SLOP = 8;
    const COMMIT_FRAC = 0.35;
    const COMMIT_VELOCITY = 0.5;
    let armed = false, dragging = false;
    let startX = 0, startY = 0, lastX = 0, lastT = 0;
    let tgt = null;   // {view, followEls, commit, bounceOnly, width}

    // 跟 setupBackInterception 的 closeOneLayer 同优先级.
    // home 没 overlay 时返回 {view:null, bounceOnly:true} — silent.
    function pickTarget() {
      const help = $("view-help");
      if (help && help.classList.contains("active")) return {
        view: help, followEls: [],
        commit: () => {
          help.classList.remove("active");
          if (location.hash === "#help") {
            try { history.replaceState(null, "",
              location.pathname + location.search); } catch (_) {}
          }
        },
      };
      const apps = $("view-apps");
      if (apps && apps.classList.contains("active")) return {
        view: apps, followEls: [],
        commit: () => apps.classList.remove("active"),
      };
      const settings = $("view-settings");
      if (settings && settings.classList.contains("active")) return {
        view: settings, followEls: [],
        commit: () => settings.classList.remove("active"),
      };
      if (document.body.classList.contains("has-session")) {
        const chat = $("view-chat");
        const head = $("chat-head");
        return {
          view: chat, followEls: head ? [head] : [],
          commit: () => { const b = $("chat-back"); if (b) b.click(); },
        };
      }
      return { view: null, followEls: [], bounceOnly: true };
    }

    function applyTransform(v) {
      if (!tgt.view) return;
      tgt.view.style.transform = v;
      for (const el of tgt.followEls) el.style.transform = v;
    }
    function applyTransition(v) {
      if (!tgt.view) return;
      tgt.view.style.transition = v;
      for (const el of tgt.followEls) el.style.transition = v;
    }
    function endTransition(targetX, onEnd) {
      applyTransition("transform 260ms cubic-bezier(0.25, 1, 0.5, 1)");
      const finished = tgt;
      let done = false;
      const fire = () => {
        if (done) return;
        done = true;
        finished.view.removeEventListener("transitionend", fire);
        finished.view.style.transition = "";
        for (const el of finished.followEls) el.style.transition = "";
        onEnd && onEnd();
      };
      finished.view.addEventListener("transitionend", fire);
      setTimeout(fire, 320);
      applyTransform(targetX);
    }

    shield.addEventListener("touchstart", (e) => {
      if (e.cancelable) e.preventDefault();
      if (e.touches.length !== 1) return;
      const t = e.touches[0];
      armed = true; dragging = false;
      startX = lastX = t.clientX;
      startY = t.clientY;
      lastT = e.timeStamp;
      tgt = pickTarget();
      if (tgt.view) tgt.width = tgt.view.offsetWidth || window.innerWidth;
    }, { passive: false });

    shield.addEventListener("touchmove", (e) => {
      if (e.cancelable) e.preventDefault();
      if (!armed || !tgt) return;
      const t = e.touches[0];
      const dx = t.clientX - startX;
      const dy = t.clientY - startY;
      if (!dragging) {
        if (Math.abs(dy) > SLOP && Math.abs(dy) > Math.abs(dx)) {
          armed = false; return;
        }
        if (Math.abs(dx) <= SLOP) return;
        dragging = true;
        if (tgt.view && !tgt.bounceOnly) applyTransition("none");
      }
      if (tgt.view && !tgt.bounceOnly) {
        applyTransform(`translateX(${Math.max(0, dx)}px)`);
      }
      lastX = t.clientX;
      lastT = e.timeStamp;
    }, { passive: false });

    function release(e) {
      if (!armed) { tgt = null; return; }
      armed = false;
      if (!dragging || !tgt.view || tgt.bounceOnly) { tgt = null; return; }
      dragging = false;
      const t = (e.changedTouches && e.changedTouches[0]) ||
                { clientX: lastX, timeStamp: lastT };
      const dx = t.clientX - startX;
      const dt = Math.max(1, (e.timeStamp || lastT) - lastT);
      const v = (t.clientX - lastX) / dt;
      const commit = dx > tgt.width * COMMIT_FRAC || v > COMMIT_VELOCITY;
      const finished = tgt;
      if (commit) {
        endTransition(`translateX(${tgt.width}px)`, () => {
          finished.commit && finished.commit();
          finished.view.style.transform = "";
          for (const el of finished.followEls) el.style.transform = "";
        });
      } else {
        endTransition("translateX(0)", () => {
          finished.view.style.transform = "";
          for (const el of finished.followEls) el.style.transform = "";
        });
      }
      tgt = null;
    }
    shield.addEventListener("touchend", release, { passive: false });
    shield.addEventListener("touchcancel", (e) => {
      if (tgt && tgt.view && dragging) {
        const finished = tgt;
        endTransition("translateX(0)", () => {
          finished.view.style.transform = "";
          for (const el of finished.followEls) el.style.transform = "";
        });
      }
      armed = false; dragging = false; tgt = null;
    }, { passive: false });
  } catch (_) {}

  let me = null;
  try { me = await api("/api/me"); } catch (_) {}
  if (me && me.mode === "hub") {
    state.hubMode = true;
    state.userId = me.user_id || null;
    state.apps = me.apps || [];
    state.oauthProviders = me.oauth_providers || [];
    document.body.classList.add("hub-mode");
    renderOAuthButtons();
    if (state.userId) {
      enterHomeOrOnboarding();
    } else {
      showView("login");
    }
    return;
  }
  // local 模式
  if (state.token) {
    tryLogin(state.token).then(enterHome).catch(() => {
      state.token = "";
      localStorage.removeItem("ccr.token");
      showView("login");
    });
  } else {
    showView("login");
  }
})();

