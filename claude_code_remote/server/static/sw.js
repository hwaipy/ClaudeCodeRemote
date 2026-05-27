// ClaudeCodeRemote service worker.
// 策略 (cache-first, 用户手动刷新才回源):
//   所有 GET (含 navigate)  默认缓存优先, 命中即返回, 缺货才下载并写入缓存.
//   req.cache === "reload" / "no-cache"  网络优先, 把新内容写回缓存.
//     浏览器把 F5 / Ctrl+R / 下拉刷新 / location.reload() 等手动操作
//     标记为 "reload" / "no-cache" — 用户主动要新版本就给新版本.
//   /api/ 和 /ws  完全不走 SW (业务数据永远新鲜).
//
// 反代前缀通过 self.registration.scope 自动获得 (注册时由 client 端按
// document.baseURI 算好), 不需要硬编码 /remote 之类.

const CACHE = "ccr-v176";   // bump 强制清掉旧 cache, PWA 重启后拿新代码.
                            // ⚠ 必须跟 app.js 顶上的 __CCR_APP_VER 同步 bump,
                            // 否则 footer 显示版本不准, 用户看不出新代码到了没.
const SCOPE_PATH = new URL(self.registration.scope).pathname;  // 末尾保证带 /
// LLM brand icons — install 时 addAll 预 cache, 首次进 USTC/DS/etc session
// 就不需要再走网络拉远程 CDN. 列表跟 _MODEL_BRANDS 的 slug 一致.
const _LLM_ICON_SLUGS = [
  "deepseek", "googlegemini", "qwen", "meta", "mistralai",
  "x", "moonshotai", "bytedance", "githubcopilot",
  "alibabadotcom", "claude", "anthropic",
];
const _OAUTH_ICON_SLUGS = ["google", "github", "gitee", "feishu", "dingtalk", "qq"];
const _LANDING_SCREENSHOTS = ["mobile-home", "desktop-chat", "mobile-chat"];
const SHELL = [
  SCOPE_PATH,
  SCOPE_PATH + "icon.svg",
  SCOPE_PATH + "manifest.webmanifest",
  ..._LLM_ICON_SLUGS.map(s => SCOPE_PATH + "static/lib/llm-icons/" + s + ".svg"),
  ..._OAUTH_ICON_SLUGS.map(s => SCOPE_PATH + "static/lib/oauth-icons/" + s + ".svg"),
  ..._LANDING_SCREENSHOTS.map(s => SCOPE_PATH + "static/lib/screenshots/" + s + ".png"),
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // 跳过 API / WS (WS 是 ws:/wss: scheme, 本身就不会过 fetch handler,
  // 这里只防御性兜底)
  if (url.pathname.startsWith(SCOPE_PATH + "api/")
      || url.pathname.startsWith(SCOPE_PATH + "ws/")
      || url.pathname === SCOPE_PATH + "ws-global") return;

  // 用户手动刷新: req.cache 是 "reload" 或 "no-cache" — 走网络, 把
  // 新版本写回缓存, 失败回退到缓存兜底.
  const isManualRefresh = req.cache === "reload" || req.cache === "no-cache";

  const cacheKey = (req.mode === "navigate") ? SCOPE_PATH : req;

  if (isManualRefresh) {
    e.respondWith(
      fetch(req)
        .then(res => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then(c => c.put(cacheKey, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match(cacheKey))
    );
    return;
  }

  // 默认: cache-first. 命中即返回 (零网络), 未命中再下载 + 写缓存.
  e.respondWith(
    caches.match(cacheKey).then(cached => cached || fetch(req).then(res => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(cacheKey, copy)).catch(() => {});
      }
      return res;
    }))
  );
});
