/**
 * 极简 Service Worker —— 只做两件事：静态资源离线可读、接口永远走网络。
 *
 * 为什么不引 vite-plugin-pwa：
 *   本项目坚持零运行时依赖，而这里需要的能力只有「按 hash 缓存静态资源」，
 *   手写 60 行足够，且行为完全可控（不会自动 precache 一堆用不上的文件）。
 *
 * 关键约定：
 *   · /api/*  一律走网络，绝不缓存 —— 会话、配置、登录态都不能陈旧；
 *   · 导航请求（刷新页面）网络优先，失败才回退缓存，避免发版后卡在旧 index.html；
 *   · 其它静态资源缓存优先 —— Vite 产物文件名带内容 hash，URL 变了自然换新。
 */
const CACHE = "mini-agent-v1";
const CORE = ["/", "/index.html", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(CORE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // 接口不缓存

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put("/index.html", copy));
          return res;
        })
        .catch(() =>
          caches
            .match("/index.html")
            .then((hit) => hit || caches.match("/"))
        )
    );
    return;
  }

  event.respondWith(
    caches.match(req).then(
      (hit) =>
        hit ||
        fetch(req).then((res) => {
          if (res.ok && res.type === "basic") {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put(req, copy));
          }
          return res;
        })
    )
  );
});
