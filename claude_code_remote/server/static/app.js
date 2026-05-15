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
  askuserById: new Map(),      // tool_use_id -> {card, questions, answers, submitted}
  activeMsgId: null,           // 当前打开的 stream message id
  blocksByIdx: new Map(),      // stream message index -> {type, msgId|toolUseId}
  // 每个 session 的 DOM + 流式状态快照：切走时缓存，切回来直接复用，免 spinner 免重渲
  sessionCache: new Map(),     // session_id -> snapshot
  maxSeq: 0,                   // 当前 session 已处理事件的最高 seq；WS 重连 dedupe 用
};
const SESSION_CACHE_MAX = 10;

// Most-recent-used cwds for the chip strip. Stored as a JSON array in
// localStorage.ccr.recentCwds, left = newest, max 10 entries. Updated on
// every successful spawn (see spawn handler).
const RECENT_CWDS_KEY = "ccr.recentCwds";
const RECENT_CWDS_MAX = 10;
function loadRecentCwds() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_CWDS_KEY) || "[]");
    return Array.isArray(v) ? v.filter(x => typeof x === "string" && x) : [];
  } catch (e) { return []; }
}
function pushRecentCwd(path) {
  const p = (path || "").trim();
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
async function browseLoad(path) {
  const list = $("modal-list");
  list.innerHTML = '<div class="modal-empty">Loading…</div>';
  try {
    const j = await api(`api/ls?path=${encodeURIComponent(path || "")}`);
    _browse.curPath = j.path;
    $("modal-crumb").textContent = j.path;
    const rows = [];
    if (j.parent !== null) {
      rows.push(`<div class="modal-row parent" data-path="${escHTML(j.parent)}"><span class="icon">↰</span><span class="name">.. (parent)</span></div>`);
    }
    for (const d of j.dirs) {
      const child = j.path === "/" ? "/" + d : j.path + "/" + d;
      rows.push(`<div class="modal-row" data-path="${escHTML(child)}"><span class="icon">📁</span><span class="name">${escHTML(d)}</span></div>`);
    }
    if (!j.dirs.length && j.parent === null) {
      rows.push('<div class="modal-empty">(no subdirectories)</div>');
    } else if (!j.dirs.length) {
      rows.push('<div class="modal-empty">(no subdirectories)</div>');
    }
    list.innerHTML = rows.join("");
    list.querySelectorAll(".modal-row").forEach(el => {
      el.addEventListener("click", () => browseLoad(el.dataset.path));
    });
  } catch (e) {
    list.innerHTML = `<div class="modal-empty err show">Load failed: ${escHTML(e.message)}</div>`;
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
$("modal-newdir").addEventListener("click", async () => {
  const parent = _browse.curPath;
  if (!parent) return;
  const name = (prompt(`Create new folder in:\n${parent}\n\nName:`) || "").trim();
  if (!name) return;
  try {
    const r = await api("api/mkdir", {
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
  if (d < 60)     return Math.max(1, Math.floor(d)) + "s";
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
  const listInactive = $("session-list-inactive");
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
  const active   = all.filter(s => !s.is_inactive);
  const inactive = all.filter(s =>  s.is_inactive);

  // Inactive section header always visible; count is empty when 0.
  inactiveBox.querySelector(".count").textContent = inactive.length ? `(${inactive.length})` : "";

  // Render active
  if (!active.length) {
    listActive.innerHTML = `<div class="session-empty">No sessions</div>`;
  } else {
    listActive.innerHTML = "";
    for (const s of active) renderOneCard(s, listActive, /*inactive=*/false);
  }
  // Render inactive
  listInactive.innerHTML = "";
  for (const s of inactive) renderOneCard(s, listInactive, /*inactive=*/true);
}

function renderOneCard(s, container, isInactiveSection) {
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
  // Top-right kebab menu. Items differ per section:
  //   Active   → Rename / Deactivate / Delete
  //   Inactive → Activate / Delete
  const menuItemsHtml = isInactiveSection
    ? `<button class="card-menu-item" role="menuitem" data-action="activate">Activate</button>
       <button class="card-menu-item card-menu-item-danger" role="menuitem" data-action="delete">Delete</button>`
    : `<button class="card-menu-item" role="menuitem" data-action="rename">Rename</button>
       <button class="card-menu-item" role="menuitem" data-action="deactivate">Deactivate</button>
       <button class="card-menu-item card-menu-item-danger" role="menuitem" data-action="delete">Delete</button>`;
  el.innerHTML = `
    <button class="card-menu-btn" aria-label="More" title="More">⋯</button>
    <div class="card-menu" hidden role="menu">${menuItemsHtml}</div>
    <div class="session-row1">
      <span class="state-dot" aria-hidden="true"></span>
      <div class="name">${escHTML(s.name || "untitled")}</div>
      ${showBadge ? `<span class="badge ${badge.cls}">${escHTML(badgeLabel)}</span>` : ""}
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
        } catch (err) { alert("Delete failed: " + err.message); }
      } else if (action === "deactivate") {
        try {
          await api(`/api/sessions/${encodeURIComponent(s.id)}/deactivate`,
                     { method: "POST", body: JSON.stringify({}) });
        } catch (err) { alert("Deactivate failed: " + err.message); }
      } else if (action === "activate") {
        try {
          await api(`/api/sessions/${encodeURIComponent(s.id)}/activate`,
                     { method: "POST", body: JSON.stringify({}) });
        } catch (err) { alert("Activate failed: " + err.message); }
      }
    });
  });

  el.addEventListener("click", () => {
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

// Close any open card-menu when clicking elsewhere (registered once).
if (!window.__cardMenuCloseBound) {
  window.__cardMenuCloseBound = true;
  document.addEventListener("mousedown", (e) => {
    document.querySelectorAll(".card-menu:not([hidden])").forEach(m => {
      if (!m.contains(e.target) && !m.previousElementSibling?.contains(e.target)) {
        m.setAttribute("hidden", "");
      }
    });
  });
}

// ---------- Inactive section collapse toggle ----------
(function setupInactiveToggle() {
  const box = $("sessions-inactive");
  if (!box) return;
  const header = box.querySelector(".inactive-toggle");
  if (!header) return;
  header.addEventListener("click", () => {
    box.classList.toggle("expanded");
  });
})();

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
  if (!$("spawn-cwd").value) $("spawn-cwd").value = state.cwd || "";
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
    state.sessionsById.clear();
    for (const s of msg.sessions || []) {
      state.sessionsById.set(s.id, _withSortKey(s, null));
    }
    renderSessionList();
  } else if (msg.type === "session_state") {
    const existing = state.sessionsById.get(msg.id);
    state.sessionsById.set(msg.id, _withSortKey(msg, existing));
    // Skip the full-list re-render if we just optimistically renamed
    // this session — the card already shows the new name; rebuilding
    // would just flash an empty cell during innerHTML="" reset.
    if (!renameInFlight.has(msg.id)) renderSessionList();
    maybeNotify(msg);
    if (msg.id === state.sessionId) syncChatStatusFromSession(msg);
  } else if (msg.type === "session_deleted") {
    state.sessionsById.delete(msg.id);
    state.sessionCache.delete(msg.id);   // DOM 缓存也清掉，session 没了
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

$("spawn-go").addEventListener("click", async () => {
  const name = $("spawn-name").value.trim();
  const cwd = $("spawn-cwd").value.trim();
  $("spawn-err").classList.remove("show");
  if (!cwd) {
    $("spawn-err").textContent = "Working directory required";
    $("spawn-err").classList.add("show");
    return;
  }
  $("spawn-go").disabled = true;
  $("spawn-go").textContent = "Starting…";
  try {
    const r = await api("/api/spawn", { method: "POST", body: JSON.stringify({ cwd, name }) });
    state.cwd = cwd;
    localStorage.setItem("ccr.cwd", cwd);
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

// ---------- New session modal ----------
(function setupNewModal() {
  const btn       = $("new-btn");
  const modal     = $("modal-new-session");
  const closeX    = $("new-modal-close");
  const cancelBtn = $("new-modal-cancel");

  function open() {
    modal.removeAttribute("hidden");
    if (!$("spawn-cwd").value) $("spawn-cwd").value = state.cwd || "";
    syncPresetChips();
    setTimeout(() => $("spawn-name").focus(), 0);
  }
  function close() {
    modal.setAttribute("hidden", "");
    $("spawn-err").classList.remove("show");
  }
  // Make it reachable from spawn-go success path so we can hide the modal
  // once we're on our way to chat view.
  window.__closeNewModal = close;

  btn.addEventListener("click", open);
  closeX.addEventListener("click", close);
  cancelBtn.addEventListener("click", close);
  modal.addEventListener("click", (e) => {
    if (e.target.id === "modal-new-session") close();   // backdrop only
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hasAttribute("hidden")) close();
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

// ---------- Session list search ----------
(function setupSearch() {
  const btn    = $("search-btn");
  const input  = $("search-input");
  const clear  = $("search-clear");
  const bar    = $("search-bar");
  const wrap   = document.querySelector(".home-top");
  const newBtn = $("new-btn");

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
    // Pin new-btn's current natural width as inline px so the transition
    // has two concrete endpoints (max-width / auto won't transition smoothly).
    if (newBtn) {
      newBtn.style.width = newBtn.getBoundingClientRect().width + "px";
      void newBtn.offsetWidth;   // force reflow
    }
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
    if (newBtn) {
      setTimeout(() => { newBtn.style.width = ""; }, 500);
    }
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
  const log = $("chat-log");
  // 把 chat-log 的子节点抽出来，DOM 引用（toolById / msgById 里的 .card / .bubble）依然有效
  const frag = document.createDocumentFragment();
  while (log.firstChild) frag.appendChild(log.firstChild);
  // LRU：已存在的先删再插，命中放在最后；超额从最旧的开始淘汰
  if (state.sessionCache.has(state.sessionId)) state.sessionCache.delete(state.sessionId);
  state.sessionCache.set(state.sessionId, {
    dom: frag,
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
  document.body.classList.add("has-session");
  renderSessionList();   // 让列表的 "当前" 高亮标记跟随切换
  state.loadingHistory = false;
  state.suppressScrollLoad = true;
  setTimeout(() => { state.suppressScrollLoad = false; }, 500);
  // 清掉上一次可能残留的 inline transform/transition，避免影响这次滑入动画
  const _chatView = $("view-chat");
  _chatView.style.transform = "";
  _chatView.style.transition = "";
  $("chat-name").textContent = name || "untitled";
  // 立即按默认显示 perm 按钮（用 manual），等 GET 回来再修正
  applyPermissionMode("manual");
  $("chat-perm").hidden = false;
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
    // cwd 显示用最新 home 数据（一般不变，但兜底）
    state.cwdShort = (cwd || "").split("/").slice(-2).join("/") || cwd || "";
    refreshChatMeta();
    refreshConvStatus();
    state.cacheHit = true;
    // overlay 不显示；立刻揭幕
    $("chat-loading").hidden = true;
    state.revealChat = () => showView("chat");
    state.revealChat();
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
  state.cwdShort = (cwd || "").split("/").slice(-2).join("/") || cwd || "";
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
  const _ld = $("chat-loading");
  _ld.classList.remove("fade-out");
  _ld.hidden = false;
  if (sessionState && sessionState !== "running") {
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}/resume`, { method: "POST" });
    } catch (e) {
      appendBubble("system", `Resume failed: ${e.message}`);
    }
  }
  let revealed = false;
  const ownId = id;
  const reveal = () => {
    if (revealed) return;
    revealed = true;
    if (state.sessionId !== ownId) return;
    const log = $("chat-log");
    setScrollTopInstant(log, log.scrollHeight);
    showView("chat");
    if (window.innerWidth >= 900) $("chat-input").focus();
  };
  state.revealChat = reveal;
  setTimeout(reveal, 800);
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

function applyPermissionMode(mode) {
  if (mode !== "manual" && mode !== "allow_all") return;
  state.permissionMode = mode;
  const btn = $("chat-perm");
  if (btn) {
    btn.hidden = !state.sessionId;
    btn.classList.toggle("allow-all", mode === "allow_all");
    btn.title = mode === "allow_all" ? "Permission: Allow all" : "Permission: Ask each time";
  }
  const menu = $("perm-menu");
  if (menu) {
    menu.querySelectorAll(".perm-menu-item").forEach(it => {
      it.classList.toggle("active", it.dataset.mode === mode);
    });
  }
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

function togglePermMenu(force) {
  const menu = $("perm-menu");
  if (!menu) return;
  const show = (typeof force === "boolean") ? force : menu.hidden;
  menu.hidden = !show;
  if (show) {
    // 对齐按钮右侧
    const btn = $("chat-perm");
    if (btn) {
      const r = btn.getBoundingClientRect();
      menu.style.top = (r.bottom + 6) + "px";
      menu.style.right = Math.max(8, window.innerWidth - r.right) + "px";
    }
  }
}

$("chat-perm").addEventListener("click", (e) => {
  e.stopPropagation();
  togglePermMenu();
});
$("perm-menu").addEventListener("click", (e) => {
  const item = e.target.closest(".perm-menu-item");
  if (!item) return;
  const mode = item.dataset.mode;
  togglePermMenu(false);
  setPermissionMode(mode);
});
document.addEventListener("click", (e) => {
  const menu = $("perm-menu");
  if (!menu || menu.hidden) return;
  if (e.target.closest("#perm-menu") || e.target.closest("#chat-perm")) return;
  togglePermMenu(false);
});

$("chat-back").addEventListener("click", () => {
  // 退到 home：把当前 session 的 DOM + state 缓存起来，下次进来直接复用
  saveCurrentSessionCache();
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  state.sessionId = null;
  document.body.classList.remove("has-session");
  $("chat-perm").hidden = true;
  togglePermMenu(false);
  $("chat-loading").hidden = true;
  renderSessionList();   // 取消"当前"高亮
  // 退出时让 textarea 等失焦，否则 iOS PWA 软键盘会留在屏幕上
  if (document.activeElement && typeof document.activeElement.blur === "function") {
    document.activeElement.blur();
  }
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
      tokens.textContent = `${outT}${outT > 1 ? "tokens" : "token"}`;
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
  if (ctx) {
    const total = state.lastInputTotal || state.lastInputTokens || 0;
    if (total) {
      const pct = total / (state.contextLimit || 200000) * 100;
      ctx.textContent = "ctx: " + pct.toFixed(1) + "%";
    } else {
      ctx.textContent = "";
    }
  }
}
// 每秒刷新一次（让 turn 计时器跑起来）
setInterval(refreshConvStatus, 1000);

// §2: keep "active N ago" labels ticking even when no new session_state
// arrives. Updates only the .ts text node — no card-DOM rebuild — so we
// don't lose focus/scroll/animation state.
setInterval(() => {
  document.querySelectorAll(".session-card").forEach((card) => {
    const sid = card.dataset.id;
    if (!sid) return;
    const sess = state.sessionsById.get(sid);
    if (!sess) return;
    const tsEl = card.querySelector(".ts");
    if (!tsEl) return;
    const txt = relTime(sess.last_activity_at) + " ago";
    if (tsEl.textContent !== txt) tsEl.textContent = txt;
  });
}, 1000);

// iOS 键盘弹出时把整个 visual viewport 下移让焦点可见；fixed body 跟随上移 = 屏幕顶。
// chat-head 钉在 visual viewport 顶端（用户可见区顶），需要正向 translate offsetTop。
function pinChatHead() {
  const head = document.getElementById("chat-head");
  if (!head) return;
  const vp = window.visualViewport;
  head.style.top = (vp ? vp.offsetTop : 0) + "px";
  // 兜底：iOS PWA 第一次聚焦输入框时 interactive-widget=resizes-content 偶尔失灵
  // （layout viewport 不缩 → chat-foot 被键盘盖住）。这里用 visualViewport 实测的
  // 遮挡量，强行给 #view-chat 加底部 padding，把 chat-foot 推上去。
  syncKbInset();
}
function syncKbInset() {
  const vp = window.visualViewport;
  if (!vp) return;
  const overlap = Math.max(0, window.innerHeight - vp.height - vp.offsetTop);
  document.documentElement.style.setProperty("--kb-inset", overlap + "px");
}
// iOS 上推 fixed 是几百 ms 的浏览器动画；visualViewport.scroll 事件触发慢，期间会晃。
// 焦点变化时启动 RAF 持续追踪，覆盖整段动画。
let _pinRaf = null;
let _pinUntil = 0;
function pinLoop() {
  pinChatHead();
  if (performance.now() < _pinUntil) {
    _pinRaf = requestAnimationFrame(pinLoop);
  } else {
    _pinRaf = null;
  }
}
function startPinTracking(ms) {
  _pinUntil = Math.max(_pinUntil, performance.now() + ms);
  if (!_pinRaf) _pinRaf = requestAnimationFrame(pinLoop);
}
document.addEventListener("focusin", () => startPinTracking(800), true);
document.addEventListener("focusout", () => startPinTracking(800), true);
if (window.visualViewport) {
  window.visualViewport.addEventListener("scroll", pinChatHead);
  window.visualViewport.addEventListener("resize", pinChatHead);
}
window.addEventListener("scroll", pinChatHead, { passive: true });
window.addEventListener("resize", pinChatHead);
// 输入框聚焦时 / 失焦时也检查
document.addEventListener("focusin", () => setTimeout(pinChatHead, 100));
document.addEventListener("focusout", () => setTimeout(pinChatHead, 100));
pinChatHead();

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
  setHistoryLoader("Loading earlier messages…");
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
      try { handleEvent(env.event, env.ts); } catch (e) { console.warn("history render error", e); }
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
    setHistoryLoader("Load failed, pull to retry");
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
      setConnDot("connected", "Connected");
      backoff = 1000;
      // 这次连接前已经处理过的最大 seq 作为 dedupe 边界：重连/缓存命中时只跳 server 重发的旧 backlog
      // 冷启动 maxSeq=0 → 边界=0 → 不会误伤 earlier 批（earlier 的 seq 比 recent 小，但都 > 0）
      state.dedupeBoundary = state.maxSeq || 0;
      state.loadingHistory = false;
      state.pendingScrollToBottomOnBacklog = state.dedupeBoundary === 0;
    });
    ws.addEventListener("close", () => {
      if (!isCurrent()) return;
      const secs = Math.round(backoff / 1000);
      setConnDot("error", `Disconnected, reconnecting in ${secs}s`);
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
    if (e) e.textContent = info.tokens ? "↓ " + info.tokens + " tok" : "";
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
    return;
  }
  body.innerHTML = renderMarkdown(text);
  renderMathIn(body);
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
  let group = state.currentToolGroup;
  if (group && group.parentNode === root && group === root.lastElementChild) return group;
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
  if (!entry.poller && entry.argsEl) {
    // 第一次拉：放 spinner 占位（后续轮询不重置，避免闪烁）
    entry.argsEl.innerHTML = '<span class="tool-spinner"></span><span class="tool-lazy-hint">loading…</span>';
    if (entry.resultEl) { entry.resultEl.hidden = true; entry.resultEl.innerHTML = ""; }
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
function handleEvent(evt, ts) {
  const t = evt && evt.type;
  if (!t) return;

  if (t === "stream_event") return handleStreamEvent(evt.event || {});
  if (t === "assistant")    return handleAssistantMessage(evt.message || {});
  if (t === "user")         return handleUserMessage(evt.message || {});
  if (t === "user_input")   return handleUserInput(evt, ts);
  if (t === "system")       return handleSystem(evt);
  if (t === "result")       return handleResult(evt, ts);
  if (t === "_ccr") {
    if (evt.subtype === "first_paint") {
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
      // 200 条全部到位，等 200ms 再淡出 overlay（防止用户看到 prepend 抖一下）
      const loadingEl = $("chat-loading");
      setTimeout(() => {
        loadingEl.classList.add("fade-out");
        setTimeout(() => {
          loadingEl.hidden = true;
          loadingEl.classList.remove("fade-out");
        }, 220);
      }, 200);
      hideThinkingPlaceholder();
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
      state.pendingScrollToBottomOnBacklog = false;
      return;
    }
    if (evt.subtype === "permission_request") return showPermissionRequest(evt);
    if (evt.subtype === "permission_resolved") return markPermissionResolved(evt);
    if (evt.subtype === "permission_mode") return applyPermissionMode(evt.mode);
    if (evt.subtype === "turn_state") return applyTurnState(evt);
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
      if (cb.name === "AskUserQuestion") {
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
    // tool_use 兜底（极少数情况下没有任何 input_json_delta，直接 stop）
    const block = state.blocksByIdx.get(ev.index);
    if (block && block.type === "tool_use") hideThinkingPlaceholder();
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
      if (b.name === "AskUserQuestion") {
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
  refreshChatMeta();
  refreshConvStatus();
}

function handleUserInput(evt, envTs) {
  if (!state.isHistoryReplay) {
    // 重置跨 message 累加器；curOutputTokens 保留上一轮终值显示，等下次 message_start 才刷新
    state.priorTurnOutput = 0;
    state.turnFresh = true;   // 本次进 session 后真的有用户发新轮次了：解锁 token/time 显示
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
}

function refreshChatMeta() {
  const meta = $("chat-meta");
  if (!meta) return;
  // chat-head 只显示 cwd；token / ctx / cost 一律走底部 conv-status
  meta.textContent = state.cwdShort || "";
  refreshConvStatus();   // 任意时刻 chat-meta 刷新都同步底部状态栏，下行 token 跟着 text_delta 实时
}

function sendUserMessage() {
  const ta = $("chat-input");
  const text = ta.value.trim();
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
  state.ws.send(JSON.stringify({ type: "user_message", content }));
  // user bubble 等 server 注入 user_input echo 时再渲染（保证刷新/resume 也能看到）
  ta.value = "";
  ta.style.height = "auto";
  clearAttachments();
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
document.querySelectorAll("#theme-toggle").forEach(b => {
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
