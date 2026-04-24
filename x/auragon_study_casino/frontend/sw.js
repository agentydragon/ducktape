// Study Casino service worker.
//
// Cache strategy:
//   - App shell (/, /main.js, /manifest.webmanifest, /icon.svg): cache-first,
//     so the app launches offline instantly.
//   - /state: NETWORK-ONLY. The PWA writes to IndexedDB locally and pushes to
//     /state; a cached GET could silently hand back stale data and defeat
//     the whole point of having a backend for cross-device sync.
//
// Bump CACHE_VERSION to invalidate the shell cache on deploy. main.js content
// hashes would be nicer but esbuild-in-Bazel doesn't emit them by default,
// and for a single-user app a manual bump is fine.

const CACHE_VERSION = "v1";
const SHELL_CACHE = `study-casino-shell-${CACHE_VERSION}`;
const SHELL_URLS = ["/", "/main.js", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === "/state") return; // network-only
  if (event.request.method !== "GET") return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      });
    })
  );
});
