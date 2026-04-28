// Kill-switch service worker: clears all old caches and unregisters itself.
// Offline mode is not needed; this ensures stale cached JS never shadows
// new deploys.

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
      .then(() => {
        return self.registration.unregister();
      })
  );
  self.clients.claim();
});
