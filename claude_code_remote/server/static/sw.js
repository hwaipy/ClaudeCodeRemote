// ClaudeCodeRemote service worker.
// 策略：
//   navigate (HTML)  网络优先，失败回缓存；保证 BUILD_ID 变更立即生效
//   其它 GET       缓存优先，未命中走网络再缓存；带 ?v= 的静态资源天然
//                    受 cache-buster 控制，文件变更后浏览器请求新 URL
//   /api/ 和 /ws    完全不走 SW
//
// 反代前缀通过 self.registration.scope 自动获得（注册时由 client 端按
// document.baseURI 算好），不需要硬编码 /remote 之类。

const CACHE = "ccr-v1";
const SCOPE_PATH = new URL(self.registration.scope).pathname;  // 末尾保证带 /
const SHELL = [
  SCOPE_PATH,
  SCOPE_PATH + "icon.svg",
  SCOPE_PATH + "manifest.webmanifest",
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
  // 跳过 API / WS（WS 是 ws:/wss: scheme，本身就不会过 fetch handler，
  // 这里只防御性兜底）
  if (url.pathname.startsWith(SCOPE_PATH + "api/")
      || url.pathname.startsWith(SCOPE_PATH + "ws/")
      || url.pathname === SCOPE_PATH + "ws-global") return;

  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(SCOPE_PATH, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(SCOPE_PATH))
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
