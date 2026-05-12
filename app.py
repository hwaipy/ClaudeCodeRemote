import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request

CLAUDE_CONFIG = Path.home() / ".claude.json"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
LAUNCHER_STATE_DIR = Path.home() / ".config" / "claude-launcher"
SESSIONS_FILE = LAUNCHER_STATE_DIR / "sessions.json"
HIDDEN_FILE = LAUNCHER_STATE_DIR / "hidden.json"  # legacy, migrated into SESSIONS_FILE

app = Flask(__name__)

NAME_RE = re.compile(r"^[^\t\n\r/\\:'\"`$<>|;&*?#]{1,64}$")
SESSION_NAME_RE = re.compile(r"^claude-[^\s/\\:'\"`$<>|;&*?#]+$")
GENERATED_SESSION_RE = re.compile(r"^claude-(.+)-[0-9a-f]{6}$")
PRESET_DIRS = [
    ("codes", str(Path.home() / "codes")),
    ("Synology/Claude", str(Path.home() / "SynologyDrive" / "Claude")),
]
DEFAULT_WORK_DIR = PRESET_DIRS[0][1]

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0e1117">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Claude Launcher">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.svg">
<title>Claude Remote Session 启动器</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  background: #0e1117;
  color: #e6edf3;
  min-height: 100vh;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: clamp(32px, 12vh, 120px) 16px max(24px, env(safe-area-inset-bottom));
}
.card {
  width: 100%;
  max-width: 460px;
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 14px;
  padding: 24px 22px;
  box-shadow: 0 8px 30px rgba(0,0,0,.35);
}
h1 { font-size: 20px; margin: 0 0 18px; }
label { display: block; font-size: 14px; color: #8b949e; margin-bottom: 6px; margin-top: 12px; }
label:first-of-type { margin-top: 0; }
input[type=text] {
  width: 100%;
  font-size: 16px;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid #30363d;
  background: #0d1117;
  color: #e6edf3;
  outline: none;
}
input[type=text]:focus { border-color: #58a6ff; }
.path-row { display: flex; gap: 8px; }
.path-row input { flex: 1; }
.btn-secondary {
  flex: 0 0 auto;
  font-size: 14px;
  font-weight: 500;
  padding: 0 16px;
  border-radius: 10px;
  border: 1px solid #30363d;
  background: #21262d;
  color: #e6edf3;
  cursor: pointer;
  white-space: nowrap;
}
.btn-secondary:hover { background: #30363d; }
.presets {
  display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px;
}
.chip {
  font-size: 13px;
  padding: 5px 11px;
  border-radius: 999px;
  border: 1px solid #30363d;
  background: transparent;
  color: #8b949e;
  cursor: pointer;
  white-space: nowrap;
  transition: all .12s;
}
.chip:hover { background: #21262d; color: #e6edf3; }
.chip.active { background: #1f6feb22; border-color: #58a6ff; color: #58a6ff; }
button.primary {
  margin-top: 18px;
  width: 100%;
  font-size: 16px;
  font-weight: 600;
  padding: 13px;
  border-radius: 10px;
  border: 0;
  background: #238636;
  color: #fff;
  cursor: pointer;
  transition: background .15s;
}
button.primary:hover:not(:disabled) { background: #2ea043; }
button.primary:disabled { background: #30363d; cursor: not-allowed; }
.status {
  margin-top: 14px;
  font-size: 14px;
  padding: 10px 12px;
  border-radius: 8px;
  display: none;
  word-break: break-all;
}
.status.ok { display: block; background: #0f2e1a; border: 1px solid #2ea04355; color: #7ee787; }
.status.err { display: block; background: #2d1115; border: 1px solid #f8514955; color: #ffa198; }
.hint { margin-top: 10px; font-size: 12px; color: #6e7681; line-height: 1.5; }

.modal-bg {
  position: fixed; inset: 0; background: rgba(0,0,0,.6);
  display: none; align-items: center; justify-content: center;
  padding: 16px; z-index: 100;
}
.modal-bg.show { display: flex; }
.modal {
  width: 100%; max-width: 520px; max-height: 80vh;
  display: flex; flex-direction: column;
  background: #161b22; border: 1px solid #30363d; border-radius: 14px;
  overflow: hidden;
}
.modal-head {
  padding: 14px 16px; border-bottom: 1px solid #30363d;
  display: flex; align-items: center; gap: 10px;
}
.modal-head .crumb {
  flex: 1; font-size: 13px; color: #8b949e;
  word-break: break-all; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.modal-head .x {
  flex: 0 0 auto; background: transparent; border: 0; color: #8b949e;
  font-size: 22px; cursor: pointer; padding: 0 4px;
}
.modal-list {
  flex: 1; overflow-y: auto; padding: 6px 0;
}
.entry {
  display: flex; align-items: center; gap: 10px;
  padding: 11px 16px; cursor: pointer; font-size: 15px;
  border-bottom: 1px solid #21262d;
}
.entry:hover { background: #21262d; }
.entry .icon { flex: 0 0 18px; color: #8b949e; }
.entry .name { flex: 1; word-break: break-all; }
.entry.parent { color: #58a6ff; }
.modal-foot {
  padding: 12px 16px; border-top: 1px solid #30363d;
  display: flex; gap: 10px;
}
.modal-foot button { flex: 1; }
.modal-foot .confirm {
  background: #238636; color: #fff; border: 0;
  padding: 11px; border-radius: 10px; font-size: 15px; font-weight: 600; cursor: pointer;
}
.modal-foot .cancel {
  background: transparent; color: #e6edf3; border: 1px solid #30363d;
  padding: 11px; border-radius: 10px; font-size: 15px; cursor: pointer;
}
.modal-empty { padding: 20px; text-align: center; color: #6e7681; font-size: 14px; }

.sessions { margin-top: 24px; }
.session-list { display: flex; flex-direction: column; gap: 8px; }
.session-group {
  font-size: 11px; font-weight: 600; color: #6e7681;
  letter-spacing: .5px; text-transform: uppercase;
  padding: 0 2px;
}
.session-card + .session-group { margin-top: 8px; }
.session-group .count { color: #4a5159; font-weight: 500; margin-left: 4px; }
.session-group.toggle {
  cursor: pointer; user-select: none;
  display: flex; align-items: center;
  min-height: 26px;  /* keep row height stable whether eye button shows or not */
}
.session-group.toggle:hover .toggle-main { color: #8b949e; }
.session-group.toggle .arrow {
  display: inline-block; width: 10px;
  transition: transform 0.5s ease;
}
.session-group.toggle.open .arrow { transform: rotate(90deg); }
.session-group.toggle .toggle-main { flex: 1; }
.session-group.toggle .hidden-inline {
  flex: 0 0 auto;
  color: #6e7681; cursor: pointer;
  padding: 4px 6px; border-radius: 6px;
  background: transparent; border: 0;
  display: inline-flex; align-items: center;
}
.session-group.toggle .hidden-inline svg {
  display: block; width: 16px; height: 16px;
}
.session-group.toggle .hidden-inline:hover { color: #e6edf3; background: #21262d; }
.session-group.toggle .hidden-inline.active { color: #58a6ff; background: #1f6feb11; }
.session-group.toggle:not(.open) .hidden-inline { display: none; }

.inactive-sentinel {
  font-size: 12px; color: #6e7681;
  padding: 12px; text-align: center;
  border: 1px dashed #21262d; border-radius: 8px;
}

.active-collapse, .inactive-collapse {
  max-height: 0;
  overflow: hidden;
  display: flex; flex-direction: column; gap: 8px;
  transition: max-height var(--anim-duration, 0.5s) linear;
}
.active-collapse.open, .inactive-collapse.open {
  max-height: var(--max-height, 9999px);
}
.session-empty {
  text-align: center; color: #6e7681; font-size: 13px;
  padding: 18px; border: 1px dashed #30363d; border-radius: 10px;
}
.session-card {
  background: #0d1117; border: 1px solid #30363d;
  border-radius: 10px; padding: 11px 13px;
  display: flex; flex-direction: column; gap: 6px;
}
.session-row1 { display: flex; align-items: center; gap: 8px; }
.session-name {
  flex: 1; min-width: 0;
  font-size: 14px; font-weight: 600;
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-name.dead { color: #6e7681; font-weight: 500; }
.badge {
  flex: 0 0 auto; font-size: 11px;
  padding: 2px 7px; border-radius: 999px;
  background: #1f6feb22; color: #58a6ff; border: 1px solid #58a6ff55;
  white-space: nowrap;
}
.badge.active { background: #0f2e1a; color: #7ee787; border-color: #2ea04388; }
.badge.idle   { background: #21262d; color: #8b949e; border-color: #30363d; }
.badge.paused { background: #3a2e0c; color: #e3b341; border-color: #bb800988; }
.badge.dead   { background: #2d111522; color: #ffa198; border-color: #f8514955; }
.badge.rc-on  { background: #1f6feb22; color: #58a6ff; border-color: #58a6ff55; }
.badge.rc-off { background: #21262d; color: #6e7681; border-color: #30363d; }
.session-act {
  flex: 0 0 auto; background: transparent; border: 0;
  color: #8b949e; cursor: pointer; padding: 4px 8px;
  border-radius: 6px; font-size: 14px; line-height: 1;
}
.session-act:disabled { opacity: 0.35; cursor: not-allowed; }
.session-act.restart:hover:not(:disabled)  { background: #1f6feb22; color: #58a6ff; }
.session-act.dismiss:hover:not(:disabled)  { background: #2d1115; color: #ffa198; }
.session-act.unhide:hover:not(:disabled)   { background: #0f2e1a; color: #7ee787; }
.session-act.open-url:hover:not(:disabled) { background: #1f6feb22; color: #58a6ff; }
.session-act.cli:hover:not(:disabled)      { background: #21262d; color: #e6edf3; }
.session-act svg { width: 14px; height: 14px; display: block; }
.session-act { display: inline-flex; align-items: center; padding: 4px 5px; }

.session-actions {
  flex: 0 0 auto;
  display: inline-flex; align-items: center; gap: 2px;
  margin-left: 4px;
}

.session-card.is-hidden { opacity: 0.55; }
.session-card.is-hidden .session-name { font-weight: 500; }

.session-card.active-idle {
  border-color: rgba(46, 160, 67, 0.4);
}
.session-card.active-busy {
  border-color: #2ea043;
  box-shadow: 0 0 10px rgba(46, 160, 67, 0.35);
}

.session-meta {
  font-size: 12px; color: #8b949e;
  display: flex; align-items: center; gap: 6px;
  white-space: nowrap; overflow: hidden;
}
.session-meta .path {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.session-meta .mid-ellipsis {
  display: flex; flex: 1; min-width: 0; overflow: hidden;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.session-meta .mid-ellipsis .start {
  overflow: hidden; text-overflow: ellipsis; min-width: 0;
}
.session-meta .mid-ellipsis .end {
  flex: 0 0 auto;
}
.toast {
  position: fixed;
  top: max(24px, env(safe-area-inset-top));
  left: 50%;
  transform: translateX(-50%) translateY(-140%);
  z-index: 200;
  background: linear-gradient(135deg, #2ea043, #238636);
  color: #fff;
  font-size: 16px;
  font-weight: 600;
  padding: 14px 22px;
  border-radius: 12px;
  box-shadow: 0 12px 40px rgba(46,160,67,.45), 0 4px 12px rgba(0,0,0,.4);
  display: flex;
  align-items: center;
  gap: 12px;
  max-width: calc(100vw - 32px);
  transition: transform .35s cubic-bezier(.2,.8,.2,1), opacity .35s;
  opacity: 0;
  pointer-events: none;
}
.toast.show {
  transform: translateX(-50%) translateY(0);
  opacity: 1;
}
.toast .check {
  flex: 0 0 28px;
  width: 28px; height: 28px;
  border-radius: 50%;
  background: rgba(255,255,255,.22);
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 18px;
}
.toast .text { word-break: break-all; line-height: 1.4; }
</style>
</head>
<body>
<div class="card">
  <h1>启动 Claude Remote Session</h1>
  <form id="f" autocomplete="off">
    <label for="name">Session 名称</label>
    <input id="name" name="name" type="text" placeholder="例如：mytask 或 我的任务"
           maxlength="64" required autofocus>

    <label for="cwd">工作目录</label>
    <div class="path-row">
      <input id="cwd" name="cwd" type="text" value="{{ default_cwd }}" required>
      <button type="button" class="btn-secondary" id="browse">浏览…</button>
    </div>
    <div class="presets">
      {% for label, path in presets %}
      <button type="button" class="chip" data-path="{{ path }}">{{ label }}</button>
      {% endfor %}
    </div>

    <button id="btn" type="submit" class="primary">启动</button>
  </form>
  <div id="status" class="status"></div>
  <div class="hint">
    会执行：<code>claude --remote-control "&lt;名称&gt; - &lt;文件夹名&gt;"</code><br>
    在 tmux session <code>claude-&lt;名称&gt;</code> 中运行。
  </div>

  <div class="sessions">
    <div class="session-list" id="sessionList">
      <div class="session-empty">加载中…</div>
    </div>
  </div>
</div>

<div class="toast" id="toast" role="status" aria-live="polite">
  <span class="check">✓</span><span class="text" id="toast-text"></span>
</div>

<div class="modal-bg" id="modal">
  <div class="modal">
    <div class="modal-head">
      <div class="crumb" id="crumb"></div>
      <button class="x" id="close" type="button">×</button>
    </div>
    <div class="modal-list" id="list"></div>
    <div class="modal-foot">
      <button class="cancel" type="button" id="cancel">取消</button>
      <button class="confirm" type="button" id="confirm">选择此目录</button>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const BASE = window.location.pathname.replace(/\/+$/, '');
const api = (p) => BASE + p;
const f = $('f'), btn = $('btn'), status = $('status'), cwdInput = $('cwd');
const toast = $('toast'), toastText = $('toast-text');
let toastTimer = null;
function showToast(msg) {
  toastText.textContent = msg;
  toast.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3000);
}

function escHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function relativeTime(ts) {
  if (!ts) return '';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  return Math.floor(diff / 86400) + ' 天前';
}
function bindInactiveSentinel(currentInactive) {
  if (inactiveSentinelObs) {
    inactiveSentinelObs.disconnect();
    inactiveSentinelObs = null;
  }
  if (!inactiveOpen) return;  // 折叠时不监听，避免误触发
  const sentinel = document.getElementById('inactiveSentinel');
  if (!sentinel) return;
  inactiveSentinelObs = new IntersectionObserver((entries) => {
    if (!entries.some(e => e.isIntersecting)) return;
    if (inactiveRenderedCount >= currentInactive.length) return;
    inactiveRenderedCount = Math.min(inactiveRenderedCount + INACTIVE_BATCH, currentInactive.length);
    inactiveSentinelObs.disconnect();
    inactiveSentinelObs = null;
    loadSessions();
  }, {threshold: 0.05});
  inactiveSentinelObs.observe(sentinel);
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((res, rej) => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); res(); }
    catch (e) { rej(e); }
    finally { document.body.removeChild(ta); }
  });
}

const sessionList = $('sessionList');
let inactiveOpen = false;
let activeOpen = false;  // true after first active-collapse expansion plays
const INACTIVE_BATCH = 10;
let inactiveRenderedCount = INACTIVE_BATCH;
let inactiveSentinelObs = null;
// 乐观放置：刚 spawn / 刚 restart 的 session 暂时放进 active 组（灰边），
// 直到它真的 bridge_connected，或者超过 OPTIMISTIC_TTL_MS。
const optimisticActive = new Map();  // tmux_session -> expiry ms
const OPTIMISTIC_TTL_MS = 60000;
function markOptimisticActive(tmuxSession) {
  if (tmuxSession) optimisticActive.set(tmuxSession, Date.now() + OPTIMISTIC_TTL_MS);
}
async function loadSessions() {
  try {
    const r = await fetch(api('/api/sessions'));
    const j = await r.json();
    if (!r.ok) {
      sessionList.innerHTML = `<div class="session-empty">${escHTML(j.error || '加载失败')}</div>`;
      return;
    }
    const arr = j.sessions || [];
    if (arr.length === 0) {
      sessionList.innerHTML = '<div class="session-empty">暂无运行中的 Session</div>';
      return;
    }
    function appBadge(s) {
      if (!s.proc_alive) return ['已退出', 'dead'];
      if (s.app_status === 'busy') return ['工作中', 'active'];
      if (s.app_status === 'idle') return ['空闲', 'idle'];
      return ['未注册', 'rc-off'];
    }
    function renderCard(s) {
      const noProc = !s.proc_alive;
      // display_name 是 "<name> - <cwd basename>"；既然第二行已显示完整路径，
      // 这里把后缀 " - <basename>" 去掉只留 name 部分。
      let name = s.display_name || s.fallback_name || s.tmux_session;
      const cwdBase = s.cwd ? s.cwd.split('/').filter(Boolean).pop() : '';
      const suffix = cwdBase ? ' - ' + cwdBase : '';
      if (suffix && name.endsWith(suffix)) name = name.slice(0, -suffix.length);
      const nameCls = (noProc || !s.remote_control) ? ' dead' : '';

      // active busy 用边框脉动；inactive 不再显示进程状态徽章（是否还活由 ⟳ 是否出现暗示）
      let cardStateCls = '';
      const badges = [];
      if (s.bridge_connected) cardStateCls = s.app_status === 'busy' ? ' active-busy' : ' active-idle';
      if (s.attached) badges.push('<span class="badge">● tmux</span>');

      const metaBits = [escHTML(relativeTime(s.created))];
      if (s.proc_pid) metaBits.push(`pid ${s.proc_pid}`);
      if (s.heartbeat_at) metaBits.push(`心跳 ${escHTML(relativeTime(s.heartbeat_at))}`);

      // 重启按钮：bridge 没连、record 里有 sessionId、且不是「乐观 active」状态时显示。
      // 乐观 active 卡片要装得像真 active（不显示重启）；进程死了也能重启；
      const showRestart = !s.bridge_connected && !s._optimistic && !!s.session_id;
      const restartBtn = showRestart
        ? `<button class="session-act restart" data-act="restart" data-session="${escHTML(s.tmux_session)}" title="--resume 同一 sessionId 拉一个新 claude（继承对话历史；若进程还在会先 kill）">⟳</button>`
        : '';
      const globeSVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>';
      const cliSVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>';
      const openBtn = s.bridge_session_id
        ? `<button class="session-act open-url" data-act="open-url" data-url="https://claude.ai/code/${escHTML(s.bridge_session_id)}" title="在 claude.ai 打开此 session">${globeSVG}</button>`
        : '';
      const attachCmd = `tmux attach -t ${s.tmux_session}`;
      const cliBtn = `<button class="session-act cli" data-act="copy-tmux" data-cmd="${escHTML(attachCmd)}" title="${escHTML(s.tmux_session)}（点击复制 attach 命令）">${cliSVG}</button>`;
      // 乐观 active 跟真 active 一样：✕ 走 kill 路径（落到 inactive），不走 destroy
      const isActiveLike = s.bridge_connected || s._optimistic;
      const dismissAct = isActiveLike ? 'kill' : 'destroy';
      const dismissTitle = isActiveLike
        ? '停止 claude（卡片落到 inactive 的「已退出」状态）'
        : '彻底删除该会话（不可恢复）';
      const actions = `<div class="session-actions">${restartBtn}${cliBtn}${openBtn}<button class="session-act dismiss" data-act="${dismissAct}" data-session="${escHTML(s.tmux_session)}" title="${escHTML(dismissTitle)}">✕</button></div>`;
      return `<div class="session-card${s.hidden ? ' is-hidden' : ''}${cardStateCls}">
        <div class="session-row1">
          <div class="session-name${nameCls}">${escHTML(name)}</div>
          ${badges.join('')}
          ${actions}
        </div>
        ${s.cwd ? (() => {
          const idx = s.cwd.lastIndexOf('/');
          const start = idx > 0 ? s.cwd.slice(0, idx) : s.cwd;
          const end   = idx > 0 ? s.cwd.slice(idx) : '';
          return `<div class="session-meta">📁 <span class="mid-ellipsis"><span class="start">${escHTML(start)}</span><span class="end">${escHTML(end)}</span></span></div>`;
        })() : ''}
        <div class="session-meta">${metaBits.join(' · ')}</div>
      </div>`;
    }
    const nowTs = Date.now();
    const active = [];
    const inactive = [];
    arr.forEach(s => {
      if (s.bridge_connected) {
        optimisticActive.delete(s.tmux_session);  // 真的 active 了，清掉乐观标记
        active.push(s);
        return;
      }
      const expiry = optimisticActive.get(s.tmux_session);
      if (expiry && nowTs < expiry) {
        s._optimistic = true;
        active.push(s);
      } else {
        if (expiry) optimisticActive.delete(s.tmux_session);
        inactive.push(s);
      }
    });
    const groups = [];
    if (active.length) {
      groups.push(`<div class="session-group">active<span class="count">· ${active.length}</span></div>`);
      const dur = (active.length * 0.2) + 's';
      const cls = activeOpen ? ' open' : '';
      groups.push(`<div class="active-collapse${cls}" id="activeCollapse" style="--anim-duration:${dur}">${active.map(renderCard).join('')}</div>`);
    }
    if (inactive.length) {
      const openCls = inactiveOpen ? ' open' : '';
      if (inactiveRenderedCount > inactive.length) inactiveRenderedCount = inactive.length;
      if (inactiveRenderedCount < INACTIVE_BATCH) inactiveRenderedCount = Math.min(INACTIVE_BATCH, inactive.length);
      const shown = inactive.slice(0, inactiveRenderedCount);
      const hasMore = inactiveRenderedCount < inactive.length;
      const dur = (Math.max(shown.length, 1) * 0.2) + 's';
      const sentinelHTML = hasMore
        ? `<div class="inactive-sentinel" id="inactiveSentinel">加载更多 ${inactive.length - inactiveRenderedCount} 个…</div>`
        : '';
      groups.push(`<div class="session-group toggle${openCls}" id="inactiveToggle"><div class="toggle-main"><span class="arrow">▸</span> inactive<span class="count">· ${inactive.length}</span></div></div>`);
      groups.push(`<div class="inactive-collapse${openCls}" id="inactiveCollapse" style="--anim-duration:${dur}">${shown.map(renderCard).join('')}${sentinelHTML}</div>`);
    }
    sessionList.innerHTML = groups.join('');
    // Measure scrollHeight to give max-height a concrete target (so the
    // linear transition has a deterministic endpoint and the visible
    // height exactly matches the cards inside).
    const ac = document.getElementById('activeCollapse');
    if (ac) ac.style.setProperty('--max-height', ac.scrollHeight + 'px');
    const ic0 = document.getElementById('inactiveCollapse');
    if (ic0) ic0.style.setProperty('--max-height', ic0.scrollHeight + 'px');
    if (ac && !activeOpen) {
      void ac.offsetHeight;
      activeOpen = true;
      ac.classList.add('open');
    }
    const it = document.getElementById('inactiveToggle');
    const ic = document.getElementById('inactiveCollapse');
    if (it && ic) it.addEventListener('click', () => {
      inactiveOpen = !inactiveOpen;
      it.classList.toggle('open', inactiveOpen);
      ic.style.setProperty('--max-height', ic.scrollHeight + 'px');
      ic.classList.toggle('open', inactiveOpen);
      const renderedNow = Math.min(inactiveRenderedCount, inactive.length);
      const animMs = Math.max(renderedNow, 1) * 200 + 200;
      pollSuppressedUntil = Math.max(pollSuppressedUntil, Date.now() + animMs);
      bindInactiveSentinel(inactive);
    });
    bindInactiveSentinel(inactive);
    const actionDefs = {
      restart: {
        confirm: null,
        url: '/api/restart',
        body: s => ({session: s}),
        successToast: (s, j) => '已恢复：' + (j.display_name || s),
        onSuccess: (s, j) => markOptimisticActive(j.tmux_session || s),
        failToast: '重启失败',
        delayReload: 800,
      },
      kill: {
        confirm: null,
        url: '/api/kill',
        body: s => ({session: s}),
        onSuccess: s => optimisticActive.delete(s),
        successToast: s => '已停止：' + s,
        failToast: '停止失败',
      },
      destroy: {
        confirm: s => '是否完全删除该会话？\\n（kill claude + tmux session + 从面板移除）',
        url: '/api/destroy',
        body: s => ({session: s}),
        successToast: s => '已删除',
        failToast: '删除失败',
      },
    };
    sessionList.querySelectorAll('.session-act').forEach(b => {
      if (b.dataset.act === 'open-url') {
        b.addEventListener('click', () => {
          const url = b.dataset.url;
          const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
          if (standalone) {
            location.href = url;
          } else {
            window.open(url, '_blank', 'noopener');
          }
        });
        return;
      }
      if (b.dataset.act === 'copy-tmux') {
        b.addEventListener('click', () => {
          copyText(b.dataset.cmd)
            .then(() => showToast('已复制：' + b.dataset.cmd))
            .catch(() => showToast('复制失败'));
        });
        return;
      }
      const def = actionDefs[b.dataset.act];
      if (!def) return;
      b.addEventListener('click', async () => {
        const session = b.dataset.session;
        if (def.confirm && !confirm(def.confirm(session))) return;
        b.disabled = true;
        try {
          const r = await fetch(api(def.url), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(def.body(session))
          });
          const j = await r.json();
          if (r.ok) {
            if (def.onSuccess) def.onSuccess(session, j);
            showToast(typeof def.successToast === 'function' ? def.successToast(session, j) : def.successToast);
            if (def.delayReload) setTimeout(loadSessions, def.delayReload);
            else loadSessions();
          } else {
            showToast(def.failToast + '：' + (j.error || ''));
            b.disabled = false;
          }
        } catch (err) {
          showToast('网络错误：' + err);
          b.disabled = false;
        }
      });
    });
  } catch (err) {
    sessionList.innerHTML = `<div class="session-empty">网络错误：${escHTML(err)}</div>`;
  }
}

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = $('name').value.trim();
  const cwd = cwdInput.value.trim();
  status.className = 'status';
  btn.disabled = true; btn.textContent = '启动中…';
  try {
    const r = await fetch(api('/api/spawn'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, cwd})
    });
    const j = await r.json();
    if (r.ok) {
      status.className = 'status';
      status.textContent = '';
      showToast('启动成功：' + (j.short_name || ''));
      $('name').value = '';
      cwdInput.value = {{ default_cwd|tojson }};
      syncPresets();
      $('name').focus();
      markOptimisticActive(j.tmux_session);
      loadSessions();
    } else {
      status.className = 'status err';
      status.textContent = '✗ ' + (j.error || '启动失败');
    }
  } catch (err) {
    status.className = 'status err';
    status.textContent = '✗ 网络错误：' + err;
  } finally {
    btn.disabled = false; btn.textContent = '启动';
  }
});

const modal = $('modal'), list = $('list'), crumb = $('crumb');
let curPath = '';

async function loadDir(path) {
  list.innerHTML = '<div class="modal-empty">加载中…</div>';
  try {
    const r = await fetch(api('/api/ls?path=' + encodeURIComponent(path)));
    const j = await r.json();
    if (!r.ok) {
      list.innerHTML = '<div class="modal-empty">' + (j.error || '加载失败') + '</div>';
      return;
    }
    curPath = j.path;
    crumb.textContent = j.path;
    const rows = [];
    if (j.parent !== null) {
      rows.push(`<div class="entry parent" data-path="${encodeURIComponent(j.parent)}">
        <span class="icon">↰</span><span class="name">.. (上一级)</span></div>`);
    }
    if (j.dirs.length === 0 && j.parent === null) {
      rows.push('<div class="modal-empty">（无子目录）</div>');
    } else if (j.dirs.length === 0) {
      rows.push('<div class="modal-empty">（无子目录）</div>');
    }
    for (const d of j.dirs) {
      const child = j.path.endsWith('/') ? j.path + d : j.path + '/' + d;
      rows.push(`<div class="entry" data-path="${encodeURIComponent(child)}">
        <span class="icon">📁</span><span class="name"></span></div>`);
    }
    list.innerHTML = rows.join('');
    let idx = j.parent !== null ? 1 : 0;
    for (const d of j.dirs) {
      list.querySelectorAll('.entry')[idx].querySelector('.name').textContent = d;
      idx++;
    }
    list.querySelectorAll('.entry').forEach(el => {
      el.addEventListener('click', () => loadDir(decodeURIComponent(el.dataset.path)));
    });
  } catch (err) {
    list.innerHTML = '<div class="modal-empty">网络错误：' + err + '</div>';
  }
}

function syncPresets() {
  const v = cwdInput.value.trim();
  document.querySelectorAll('.chip').forEach(c => {
    c.classList.toggle('active', c.dataset.path === v);
  });
}
document.querySelectorAll('.chip').forEach(c => {
  c.addEventListener('click', () => {
    cwdInput.value = c.dataset.path;
    syncPresets();
  });
});
cwdInput.addEventListener('input', syncPresets);
syncPresets();

$('browse').addEventListener('click', () => {
  modal.classList.add('show');
  loadDir(cwdInput.value.trim() || '~');
});
$('close').addEventListener('click', () => modal.classList.remove('show'));
$('cancel').addEventListener('click', () => modal.classList.remove('show'));
$('confirm').addEventListener('click', () => {
  cwdInput.value = curPath;
  modal.classList.remove('show');
});
modal.addEventListener('click', (e) => {
  if (e.target === modal) modal.classList.remove('show');
});

let pollTimer = null;
let pollSuppressedUntil = 0;
function startPolling() {
  if (pollTimer || document.visibilityState !== 'visible') return;
  pollTimer = setInterval(() => {
    if (Date.now() < pollSuppressedUntil) return;
    loadSessions();
  }, 5000);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    loadSessions();
    startPolling();
  } else {
    stopPolling();
  }
});

loadSessions();
startPolling();

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(err => console.warn('SW register failed', err));
  });
}
</script>
</body>
</html>
"""


def pre_trust_directory(path: str) -> None:
    """Mark the directory as trusted in ~/.claude.json so the workspace
    trust prompt is skipped on first launch."""
    if not CLAUDE_CONFIG.exists():
        return
    try:
        with CLAUDE_CONFIG.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    projects = data.setdefault("projects", {})
    proj = projects.setdefault(path, {})
    if proj.get("hasTrustDialogAccepted") is True:
        return
    proj["hasTrustDialogAccepted"] = True
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".claude.json.", dir=str(CLAUDE_CONFIG.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CLAUDE_CONFIG)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _migrate_from_tmux() -> list:
    """One-time migration: scan tmux for claude-* sessions and import them
    as records. Honor legacy hidden.json if present."""
    tmux = shutil.which("tmux") or "/usr/bin/tmux"
    fmt = "#{session_name}|#{session_created}|#{pane_pid}|#{pane_current_path}"
    r = subprocess.run([tmux, "list-sessions", "-F", fmt],
                       capture_output=True, text=True)
    records = []
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4 or not parts[0].startswith("claude-"):
                continue
            name, created, pane_pid_str, pane_cwd = parts
            info = None
            try:
                info = inspect_pane(int(pane_pid_str))
            except ValueError:
                pass
            try:
                created_i = int(created)
            except ValueError:
                created_i = 0
            records.append({
                "tmux_session": name,
                "display_name": (info or {}).get("display_name") or "",
                "cwd": (info or {}).get("cwd") or pane_cwd or "",
                "created_at": created_i,
                "hidden": False,
            })
    legacy_hidden = set()
    try:
        with HIDDEN_FILE.open("r", encoding="utf-8") as fh:
            legacy_hidden = set(json.load(fh).get("hidden", []))
    except (OSError, json.JSONDecodeError):
        pass
    for rec in records:
        if rec["tmux_session"] in legacy_hidden:
            rec["hidden"] = True
    return records


def load_records() -> list:
    if not SESSIONS_FILE.exists():
        migrated = _migrate_from_tmux()
        save_records(migrated)
        return migrated
    try:
        with SESSIONS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("sessions", [])
    except (OSError, json.JSONDecodeError):
        return []


def save_records(records: list) -> None:
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="sessions.", dir=str(SESSIONS_FILE.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump({"sessions": records}, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, SESSIONS_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def resolve_path(raw: str) -> Path:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("路径为空")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise ValueError("请使用绝对路径（以 / 开头），或 ~ 开头")
    p = Path(expanded).resolve()
    if not p.exists():
        raise ValueError(f"路径不存在：{p}")
    if not p.is_dir():
        raise ValueError(f"不是目录：{p}")
    return p


def _read_cmdline(pid: int):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw.rstrip(b"\x00").split(b"\x00")
    return [p.decode("utf-8", errors="replace") for p in parts]


def _walk_descendants(pid: int, depth: int = 0, max_depth: int = 4):
    if depth > max_depth:
        return
    yield pid
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as fh:
            children = fh.read().split()
    except OSError:
        return
    for c in children:
        try:
            yield from _walk_descendants(int(c), depth + 1, max_depth)
        except ValueError:
            continue


def _read_proc_stat(pid: int):
    """Return (state, starttime) from /proc/<pid>/stat, or (None, None)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
    except OSError:
        return None, None
    rparen = data.rfind(")")
    if rparen == -1:
        return None, None
    rest = data[rparen + 1:].split()
    if len(rest) < 20:
        return None, None
    return rest[0], rest[19]  # state (field 3), starttime (field 22)


def _find_session_jsonl(session_id: str):
    """Locate the conversation log for a given sessionId."""
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None
    for child in projects.iterdir():
        if not child.is_dir():
            continue
        candidate = child / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def _read_rc_session(pid: int, proc_starttime: str):
    """Read ~/.claude/sessions/<pid>.json and verify procStart matches.
    Returns dict with {status, bridge_connected, bridge_session_id, updated_at, session_id} or None."""
    path = CLAUDE_SESSIONS_DIR / f"{pid}.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if proc_starttime and data.get("procStart") and str(data["procStart"]) != proc_starttime:
        return None  # stale (PID reused)
    updated_at_ms = data.get("updatedAt") or 0
    bridge_id = data.get("bridgeSessionId")
    return {
        "status": data.get("status"),
        "bridge_connected": bool(bridge_id),
        "bridge_session_id": bridge_id,
        "updated_at": int(updated_at_ms / 1000) if updated_at_ms else 0,
        "session_id": data.get("sessionId"),
    }


def inspect_pane(pane_pid: int):
    """Inspect a tmux pane's process tree, preferring `claude --remote-control`.
    Returns dict {pid, state, cwd, cmd_name, remote_control, display_name} or None."""
    claude_rc = None
    claude_other = None
    other = None
    for pid in _walk_descendants(pane_pid):
        cmdline = _read_cmdline(pid)
        if not cmdline:
            continue
        is_claude = os.path.basename(cmdline[0]) == "claude"
        if is_claude and "--remote-control" in cmdline:
            claude_rc = (pid, cmdline)
            break
        if is_claude and claude_other is None:
            claude_other = (pid, cmdline)
        if other is None:
            other = (pid, cmdline)

    chosen = claude_rc or claude_other or other
    if not chosen:
        return None
    pid, cmdline = chosen

    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = None

    display_name = None
    if claude_rc:
        idx = cmdline.index("--remote-control")
        if idx + 1 < len(cmdline):
            display_name = cmdline[idx + 1]

    state, starttime = _read_proc_stat(pid)
    rc_session = _read_rc_session(pid, starttime) if starttime else None

    return {
        "pid": pid,
        "state": state or "",
        "cwd": cwd,
        "cmd_name": os.path.basename(cmdline[0]),
        "remote_control": claude_rc is not None,
        "display_name": display_name,
        "app_status": (rc_session or {}).get("status"),
        "bridge_connected": bool((rc_session or {}).get("bridge_connected")),
        "bridge_session_id": (rc_session or {}).get("bridge_session_id"),
        "heartbeat_at": (rc_session or {}).get("updated_at") or 0,
        "rc_session_id": (rc_session or {}).get("session_id"),
    }


def list_claude_sessions(include_hidden: bool = False):
    records = load_records()
    tmux = shutil.which("tmux") or "/usr/bin/tmux"
    fmt = "#{session_name}|#{session_attached}|#{pane_pid}|#{pane_current_path}"
    tr = subprocess.run([tmux, "list-sessions", "-F", fmt],
                        capture_output=True, text=True)
    tmux_live = {}
    if tr.returncode == 0:
        for line in tr.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            tmux_live[parts[0]] = {
                "attached": parts[1] == "1",
                "pane_pid": parts[2],
                "pane_cwd": parts[3],
            }

    sessions = []
    records_dirty = False
    for rec in records:
        name = rec.get("tmux_session", "")
        if not name:
            continue

        live = tmux_live.get(name)
        info = None
        if live:
            try:
                info = inspect_pane(int(live["pane_pid"]))
            except ValueError:
                pass

        observed_sid = (info or {}).get("rc_session_id")
        if observed_sid and rec.get("sessionId") != observed_sid:
            rec["sessionId"] = observed_sid
            records_dirty = True

        is_hidden = bool(rec.get("hidden"))
        if is_hidden and not include_hidden:
            continue

        m = GENERATED_SESSION_RE.match(name)
        fallback_name = m.group(1) if m else name

        sessions.append({
            "tmux_session": name,
            "display_name": rec.get("display_name") or (info or {}).get("display_name") or "",
            "fallback_name": fallback_name,
            "cwd": (info or {}).get("cwd") or (live or {}).get("pane_cwd") or rec.get("cwd") or "",
            "created": rec.get("created_at") or 0,
            "attached": bool((live or {}).get("attached")),
            "tmux_alive": live is not None,
            "proc_alive": info is not None,
            "proc_pid": (info or {}).get("pid") or 0,
            "proc_state": (info or {}).get("state") or "",
            "proc_cmd": (info or {}).get("cmd_name") or "",
            "remote_control": bool((info or {}).get("remote_control")),
            "app_status": (info or {}).get("app_status"),
            "bridge_connected": bool((info or {}).get("bridge_connected")),
            "bridge_session_id": (info or {}).get("bridge_session_id"),
            "heartbeat_at": (info or {}).get("heartbeat_at") or 0,
            "hidden": is_hidden,
            "session_id": rec.get("sessionId") or "",
        })

    if records_dirty:
        save_records(records)

    sessions.sort(key=lambda s: -s["created"])
    return sessions


@app.route("/")
def index():
    return render_template_string(PAGE, default_cwd=DEFAULT_WORK_DIR, presets=PRESET_DIRS)


MANIFEST_JSON = json.dumps({
    "name": "Claude Remote Session 启动器",
    "short_name": "Claude Launcher",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0e1117",
    "theme_color": "#0e1117",
    "lang": "zh-CN",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
    ],
}, ensure_ascii=False)

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="96" fill="#0e1117"/>
<g fill="none" stroke="#58a6ff" stroke-width="64" stroke-linecap="round" stroke-linejoin="round">
<polyline points="112,140 232,256 112,372"/>
<line x1="272" y1="372" x2="400" y2="372"/>
</g>
</svg>"""

SW_JS = """const CACHE = 'claude-launcher-v2';
const SHELL = ['/', '/icon.svg', '/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put('/', copy)).catch(() => {});
        return res;
      }).catch(() => caches.match('/'))
    );
    return;
  }

  e.respondWith(
    caches.match(req).then(cached => cached || fetch(req).then(res => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
      }
      return res;
    }))
  );
});
"""


@app.route("/manifest.webmanifest")
def manifest():
    return Response(MANIFEST_JSON, mimetype="application/manifest+json")


@app.route("/icon.svg")
def icon_svg():
    return Response(ICON_SVG, mimetype="image/svg+xml")


@app.route("/sw.js")
def service_worker():
    resp = Response(SW_JS, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/ls")
def ls():
    raw = request.args.get("path", "").strip() or DEFAULT_WORK_DIR
    try:
        p = resolve_path(raw)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        dirs = sorted(
            (e.name for e in os.scandir(p) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")),
            key=str.lower,
        )
    except PermissionError:
        return jsonify(error=f"无权限读取：{p}"), 403
    parent = None if p == p.parent else str(p.parent)
    return jsonify(path=str(p), parent=parent, dirs=dirs)


@app.route("/api/sessions")
def sessions():
    include_hidden = request.args.get("include_hidden", "").lower() in ("1", "true", "yes")
    records = load_records()
    return jsonify(
        sessions=list_claude_sessions(include_hidden=include_hidden),
        hidden_count=sum(1 for r in records if r.get("hidden")),
    )


@app.route("/api/hide", methods=["POST"])
def hide():
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    hide_flag = bool(data.get("hide", True))
    if not SESSION_NAME_RE.match(session):
        return jsonify(error="非法 session 名"), 400
    records = load_records()
    changed = False
    for rec in records:
        if rec.get("tmux_session") == session:
            rec["hidden"] = hide_flag
            changed = True
            break
    if not changed:
        return jsonify(error="未找到该 session 记录"), 404
    save_records(records)
    return jsonify(ok=True, hidden=hide_flag,
                   hidden_count=sum(1 for r in records if r.get("hidden")))


@app.route("/api/destroy", methods=["POST"])
def destroy():
    """User-facing 'remove': SIGTERM claude + tmux kill-session, then mark
    the record hidden so the UI no longer shows it. The record itself
    (and claude's own session/jsonl files) are preserved on disk."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not SESSION_NAME_RE.match(session):
        return jsonify(error="非法 session 名"), 400

    tmux = shutil.which("tmux") or "/usr/bin/tmux"

    pr = subprocess.run(
        [tmux, "list-panes", "-t", session, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if pr.returncode == 0:
        pids = [l.strip() for l in pr.stdout.splitlines() if l.strip()]
        if pids:
            try:
                pane_pid = int(pids[0])
                target_pid = pane_pid
                for pid in _walk_descendants(pane_pid):
                    cmdline = _read_cmdline(pid)
                    if cmdline and os.path.basename(cmdline[0]) == "claude":
                        target_pid = pid
                        break
                try:
                    os.kill(target_pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            except ValueError:
                pass

    subprocess.run(
        [tmux, "kill-session", "-t", session],
        capture_output=True, text=True,
    )

    records = load_records()
    found = False
    for rec in records:
        if rec.get("tmux_session") == session:
            rec["hidden"] = True
            found = True
            break
    if found:
        save_records(records)
    return jsonify(ok=True, hidden=found)


@app.route("/api/dismiss", methods=["POST"])
def dismiss():
    """Merged ✕ action: stop the claude process (SIGTERM, leaving the tmux
    window as a tombstone) AND mark the record as hidden so the card
    disappears from the panel. User can still recover via the
    「显示已隐藏」 toggle + ↩."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not SESSION_NAME_RE.match(session):
        return jsonify(error="非法 session 名"), 400

    tmux = shutil.which("tmux") or "/usr/bin/tmux"

    # Kill phase — best-effort: ok if tmux session is gone or proc dead.
    pr = subprocess.run(
        [tmux, "list-panes", "-t", session, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if pr.returncode == 0:
        pids = [l.strip() for l in pr.stdout.splitlines() if l.strip()]
        if pids:
            try:
                pane_pid = int(pids[0])
                subprocess.run(
                    [tmux, "set-option", "-wt", session, "remain-on-exit", "on"],
                    capture_output=True, text=True,
                )
                target_pid = pane_pid
                for pid in _walk_descendants(pane_pid):
                    cmdline = _read_cmdline(pid)
                    if cmdline and os.path.basename(cmdline[0]) == "claude":
                        target_pid = pid
                        break
                try:
                    os.kill(target_pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            except ValueError:
                pass

    # Hide phase
    records = load_records()
    found = False
    for rec in records:
        if rec.get("tmux_session") == session:
            rec["hidden"] = True
            found = True
            break
    if not found:
        return jsonify(error="未找到该 session 记录"), 404
    save_records(records)
    return jsonify(ok=True,
                   hidden_count=sum(1 for r in records if r.get("hidden")))


@app.route("/api/kill", methods=["POST"])
def kill():
    """Stop the claude process inside the tmux session, but keep the tmux
    window alive (via remain-on-exit) so the card stays visible as a
    dead/exited entry. User can then ✕ to hide it."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not SESSION_NAME_RE.match(session):
        return jsonify(error="非法 session 名（必须以 claude- 开头且不含特殊字符）"), 400
    tmux = shutil.which("tmux") or "/usr/bin/tmux"

    pr = subprocess.run(
        [tmux, "list-panes", "-t", session, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    if pr.returncode != 0:
        return jsonify(error=pr.stderr.strip() or "未找到 session"), 404
    pids = [l.strip() for l in pr.stdout.splitlines() if l.strip()]
    if not pids:
        return jsonify(error="该 session 没有 pane"), 500
    try:
        pane_pid = int(pids[0])
    except ValueError:
        return jsonify(error="pane_pid 解析失败"), 500

    subprocess.run(
        [tmux, "set-option", "-wt", session, "remain-on-exit", "on"],
        capture_output=True, text=True,
    )

    target_pid = pane_pid
    for pid in _walk_descendants(pane_pid):
        cmdline = _read_cmdline(pid)
        if cmdline and os.path.basename(cmdline[0]) == "claude":
            target_pid = pid
            break

    try:
        os.kill(target_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # already dead
    except PermissionError:
        return jsonify(error=f"无权限终止 pid {target_pid}"), 403

    return jsonify(ok=True, killed_pid=target_pid)


@app.route("/api/restart", methods=["POST"])
def restart():
    """Restart a session from its persisted record. Works regardless of
    whether the claude process is still alive, the tmux session still
    exists, or the record is hidden — as long as the record has a
    sessionId and the cwd still resolves."""
    data = request.get_json(silent=True) or {}
    session = (data.get("session") or "").strip()
    if not SESSION_NAME_RE.match(session):
        return jsonify(error="非法 session 名"), 400

    records = load_records()
    rec = next((r for r in records if r.get("tmux_session") == session), None)
    if rec is None:
        return jsonify(error=f"未找到 record：{session}"), 404

    session_id = rec.get("sessionId")
    if not session_id:
        return jsonify(error="record 中没有 sessionId，无法 --resume"), 400

    display_name = rec.get("display_name") or session
    cwd = rec.get("cwd") or ""
    if not cwd or not os.path.isdir(cwd):
        return jsonify(error=f"工作目录无效：{cwd}"), 400

    # 有 .jsonl 就 --resume 接上历史；没有（空会话）就 --session-id 起个新的但保留同一 ID
    jsonl = _find_session_jsonl(session_id)
    resume_mode = "resume" if jsonl else "fresh"

    tmux = shutil.which("tmux") or "/usr/bin/tmux"
    claude = shutil.which("claude") or "/home/hwaipy/.local/nodejs/bin/claude"

    # 不管有没有都尝试 kill；不存在就忽略
    subprocess.run(
        [tmux, "kill-session", "-t", session],
        capture_output=True, text=True,
    )

    pre_trust_directory(cwd)

    if resume_mode == "resume":
        inner = (
            f"{shlex.quote(claude)} --remote-control {shlex.quote(display_name)}"
            f" --resume {shlex.quote(session_id)}"
        )
    else:
        inner = (
            f"{shlex.quote(claude)} --remote-control {shlex.quote(display_name)}"
            f" --session-id {shlex.quote(session_id)}"
        )
    sr = subprocess.run(
        [tmux, "new-session", "-d", "-s", session, "-c", cwd, inner],
        capture_output=True, text=True,
    )
    if sr.returncode != 0:
        return jsonify(error=f"tmux 启动失败：{sr.stderr.strip() or sr.stdout.strip()}"), 500

    # 重启后顺手取消 hidden —— 用户既然在恢复，肯定想看到它
    if rec.get("hidden"):
        rec["hidden"] = False
        save_records(records)

    return jsonify(
        ok=True,
        tmux_session=session,
        display_name=display_name,
        resumed_session_id=session_id,
        resume_mode=resume_mode,
        jsonl=str(jsonl) if jsonl else None,
    )


@app.route("/api/spawn", methods=["POST"])
def spawn():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not NAME_RE.match(name):
        return jsonify(error="名称长度需 1-64，且不能含 tab/换行 或 / \\ : ' \" ` $ < > | ; & * ? #"), 400

    try:
        cwd = resolve_path(data.get("cwd") or DEFAULT_WORK_DIR)
    except ValueError as e:
        return jsonify(error=f"工作目录无效：{e}"), 400

    claude = shutil.which("claude") or "/home/hwaipy/.local/nodejs/bin/claude"
    tmux = shutil.which("tmux") or "/usr/bin/tmux"

    session_name = f"{name} - {cwd.name}"
    tmux_safe_name = re.sub(r"\s+", "_", name)

    for _ in range(5):
        tmux_session = f"claude-{tmux_safe_name}-{secrets.token_hex(3)}"
        exists = subprocess.run(
            [tmux, "has-session", "-t", tmux_session],
            capture_output=True,
        ).returncode == 0
        if not exists:
            break
    else:
        return jsonify(error="无法生成唯一 tmux session 名（连续 5 次冲突）"), 500

    pre_trust_directory(str(cwd))

    inner = f"{shlex.quote(claude)} --remote-control {shlex.quote(session_name)}"
    cmd = [tmux, "new-session", "-d", "-s", tmux_session, "-c", str(cwd), inner]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return jsonify(error=f"tmux 启动失败：{r.stderr.strip() or r.stdout.strip()}"), 500

    records = load_records()
    records.append({
        "tmux_session": tmux_session,
        "display_name": session_name,
        "cwd": str(cwd),
        "created_at": int(time.time()),
        "hidden": False,
    })
    save_records(records)

    return jsonify(
        ok=True,
        short_name=session_name,
        tmux_session=tmux_session,
        message=f"已启动 '{session_name}'，工作目录 {cwd}（tmux: {tmux_session}）。"
                f" 用 `tmux attach -t {tmux_session}` 接入。",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=1880, debug=False)
