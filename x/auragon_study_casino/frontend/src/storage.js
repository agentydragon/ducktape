// Offline-first state storage.
//
// Reads:  backend first (cross-device sync), IndexedDB fallback on network fail.
// Writes: IndexedDB immediately, backend in background with ETag-based conflict
//         detection. On 412 Precondition Failed (another device wrote between
//         our read and our write) we pull the server copy into IDB and reload
//         the page — last-device-wins, no merge. A per-field merge would be
//         the "right" answer but this is a single-user app, so the simpler
//         behavior is fine.
//
// The state blob is stored under a single IndexedDB key `state-v1`. Along with
// it we track `etag-v1`, the ETag last seen from the backend, so PUT can use
// If-Match and the backend can reject stale writes. When we have never seen a
// server ETag (first-launch-offline-then-online) we send If-Match: "empty" so
// the first PUT can't blind-clobber state another device already wrote — the
// backend's empty-state ETag is "empty", so this matches a genuinely-empty
// server and 412s otherwise.

import { get as idbGet, set as idbSet } from "idb-keyval";

const STATE_KEY = "state-v1";
const ETAG_KEY = "etag-v1";
const EMPTY_ETAG = '"empty"';
const BACKEND_URL = "/state";

export async function loadState() {
  try {
    const response = await fetch(BACKEND_URL, { credentials: "same-origin" });
    if (response.ok) {
      const etag = response.headers.get("ETag");
      const remote = await response.json();
      await idbSet(STATE_KEY, remote);
      if (etag) await idbSet(ETAG_KEY, etag);
      return remote;
    }
    // 404 on first run — no state yet on the backend. Fall through to IDB.
  } catch (e) {
    // Offline or backend down — fall back to IndexedDB.
  }
  return (await idbGet(STATE_KEY)) ?? null;
}

let pendingSync = null;

export async function saveState(state) {
  await idbSet(STATE_KEY, state);
  // Coalesce rapid successive saves into a single backend PUT.
  if (!pendingSync) {
    pendingSync = (async () => {
      try {
        // Let the microtask queue settle so the last saveState() wins.
        await new Promise((r) => setTimeout(r, 0));
        await pushToBackend(await idbGet(STATE_KEY));
      } catch (e) {
        // pushToBackend swallows network errors, so reaching here means
        // something unexpected (IDB failure, etc.). Log rather than letting
        // it surface as an unhandled rejection — the next saveState will
        // retry the push anyway.
        console.error("sync failed", e);
      } finally {
        pendingSync = null;
      }
    })();
  }
}

async function pushToBackend(state) {
  // Default to the backend's empty-store ETag so we always send If-Match.
  // Omitting it would let an offline-first-launch blind-overwrite whatever
  // another device wrote while we were disconnected.
  const etag = (await idbGet(ETAG_KEY)) ?? EMPTY_ETAG;
  const headers = { "Content-Type": "application/json", "If-Match": etag };
  try {
    const response = await fetch(BACKEND_URL, {
      method: "PUT",
      credentials: "same-origin",
      headers,
      body: JSON.stringify(state),
    });
    if (response.ok) {
      const newEtag = response.headers.get("ETag");
      if (newEtag) await idbSet(ETAG_KEY, newEtag);
    } else if (response.status === 412) {
      // Conflict — another device wrote since our last read. Reload + retry.
      // Simplest correct behavior: pull remote, let the user's next edit
      // overwrite. Since this is a single-user app, conflicts only happen
      // across devices. We accept "last device to edit wins" — the
      // alternative (per-field updatedAt merge) is needlessly complex.
      const remote = await (await fetch(BACKEND_URL, { credentials: "same-origin" })).json();
      await idbSet(STATE_KEY, remote);
      // Note: the caller's in-memory React state is now stale. A full reload
      // is the cleanest recovery and rare in practice.
      window.location.reload();
    }
  } catch (e) {
    // Offline — IDB already has the write; next successful saveState will
    // push the latest blob.
  }
}
