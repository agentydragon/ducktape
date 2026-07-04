// Content-addressed blob store for repo.ts. A git blob's content is immutable for its sha, so a
// blob is safe to keep forever AND across reloads — that's what lets us persist it. Two tiers:
//   - an in-memory Map (always), and
//   - localStorage keyed by sha (best-effort).
// The UI runs in a sandboxed cross-origin iframe where localStorage may be absent or throw on touch
// (see main.tsx), so every localStorage access is guarded — but never silently: a failure degrades
// to the memory tier and is logged, never swallowed. No invalidation: an entry keyed by sha can
// never be stale (content-addressed), so nothing here expires — clearBlobCache is only for tests /
// an explicit reset.

import { logger } from "./log.ts";

const log = logger("blob-cache");
const memory = new Map<string, string>();
const PREFIX = "haku-blob:";

// Probe once at load: the working Storage, or null if touching it throws (disabled iframe, private
// mode) or it's absent (node/SSR). After this, `store === null` means "memory only".
const store: Storage | null = (() => {
  try {
    const s = globalThis.localStorage;
    const probe = `${PREFIX}\0probe`;
    s.setItem(probe, "1");
    s.removeItem(probe);
    return s;
  } catch (e) {
    log.info("localStorage unavailable, using in-memory cache only", e);
    return null;
  }
})();

// Content for a sha, promoting a localStorage-only hit (a prior session's) into memory; undefined
// if uncached anywhere.
export function getBlob(sha: string): string | undefined {
  const hit = memory.get(sha);
  if (hit !== undefined) return hit;
  if (store === null) return undefined;
  try {
    const stored = store.getItem(PREFIX + sha);
    if (stored === null) return undefined;
    memory.set(sha, stored);
    return stored;
  } catch (e) {
    log.warn(`localStorage read failed for ${sha}`, e);
    return undefined; // treat as a miss
  }
}

export const hasBlob = (sha: string): boolean => getBlob(sha) !== undefined;

export function setBlob(sha: string, content: string): void {
  memory.set(sha, content);
  if (store === null) return;
  try {
    store.setItem(PREFIX + sha, content);
  } catch {
    // Likely quota — drop our persisted blobs and retry once (a real recovery, not a swallow).
    evictPersisted();
    try {
      store.setItem(PREFIX + sha, content);
    } catch (e) {
      // Still failing: the memory tier holds it, so we only forfeit cross-reload persistence here.
      log.warn(`could not persist ${sha} to localStorage (kept in memory)`, e);
    }
  }
}

// Remove only our keys, leaving the rest of localStorage alone.
function evictPersisted(): void {
  if (store === null) return;
  try {
    const ours: string[] = [];
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (key !== null && key.startsWith(PREFIX)) ours.push(key);
    }
    for (const key of ours) store.removeItem(key);
  } catch (e) {
    log.warn("localStorage eviction failed", e);
  }
}

// Full reset — memory + persisted. For tests and an explicit session reset; production never needs
// it (blobs are immutable by sha).
export function clearBlobCache(): void {
  memory.clear();
  evictPersisted();
}
