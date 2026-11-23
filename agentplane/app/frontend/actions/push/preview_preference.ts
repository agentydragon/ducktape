// The page and service worker share this per-browser preference, even when no app tab is open.
const CACHE_NAME = "agentplane-action-notification-previews";
const PREFERENCE_KEY = "/.agentplane/action-notification-previews";

export async function notificationPreviewsEnabled(): Promise<boolean> {
  try {
    return (await caches.match(PREFERENCE_KEY, { cacheName: CACHE_NAME })) !== undefined;
  } catch {
    return false;
  }
}

export async function setNotificationPreviewsEnabled(enabled: boolean): Promise<void> {
  if (enabled) {
    const cache = await caches.open(CACHE_NAME);
    await cache.put(PREFERENCE_KEY, new Response());
  } else {
    await caches.delete(CACHE_NAME);
  }
}
