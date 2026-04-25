// CRDT-backed multi-device sync for the Study Casino.
//
// Architecture: one Y.Doc per running tab, persisted to IndexedDB by
// `y-indexeddb`, synced to the server's canonical Y.Doc via HTTP polling
// against POST /sync. The server speaks the same Yrs/Yjs binary update
// format and gates writes through the Python validators in validators.py;
// this module is the wire-format mirror.
//
// The doc shape is documented in `x/auragon_study_casino/doc_shape.py` —
// keep both in lockstep when adding fields:
//
//   doc.getMap("balance")    : { credits: number, tokens: number }
//   doc.getMap("active")     : current live session, empty when none
//   doc.getMap("sessions")   : id -> Y.Map({ subject, seconds, ended_at_ms })
//   doc.getMap("prizes")     : id -> Y.Map({ name, cost })
//   doc.getArray("prize_log"): Y.Map({ id, name, cost, at_ms })
//
// Error policy (called out in CLAUDE-PR review): the frontend never
// silently swallows sync failures. Every network or validation error
// surfaces through `syncStatus`; the SyncBanner UI renders that store.

import * as Y from "yjs";
import { IndexeddbPersistence } from "y-indexeddb";

const SYNC_URL = "/sync";
const POLL_INTERVAL_MS = 30_000;
const PUSH_DEBOUNCE_MS = 200;
const IDB_DB_NAME = "casino-doc-v1";
const ORIGIN_REMOTE = Symbol("remote");

const DEFAULT_PRIZES = [
  ["p1", "Anime episode break", 30],
  ["p2", "Nice coffee shop trip", 60],
  ["p3", "Takeout night", 120],
  ["p4", "Nice dinner out with Rai", 240],
  ["p5", "Buy a new game", 600],
  ["p6", "Weekend getaway", 1800],
];

function bytesToB64(bytes) {
  // Quadratic string concat blows up CPU/memory once a sync push gets into
  // the multi-MB range (the doc + per-tab IndexedDB history can grow there);
  // chunk into 32 KiB blocks and join once.
  const CHUNK = 0x8000;
  const parts = [];
  for (let i = 0; i < bytes.length; i += CHUNK) {
    parts.push(String.fromCharCode(...bytes.subarray(i, i + CHUNK)));
  }
  return btoa(parts.join(""));
}

function b64ToBytes(b64) {
  if (!b64) return new Uint8Array();
  const s = atob(b64);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

// Tiny observable for the SyncStatus state. We don't pull in zustand for
// just this; React subscribes via `useSyncStatus()` in y_hooks.js.
class SyncStatusStore {
  constructor() {
    this.state = { kind: "syncing" };
    this.listeners = new Set();
  }
  set(state) {
    this.state = state;
    for (const l of this.listeners) l(state);
  }
  subscribe(l) {
    this.listeners.add(l);
    l(this.state);
    return () => this.listeners.delete(l);
  }
}

class CasinoSync {
  constructor() {
    this.doc = new Y.Doc();
    // Declare the typed roots up front so `Y.applyUpdate` populates the
    // right handles. Mirrors the pycrdt `Casino` wrapper on the server.
    this.balance = this.doc.getMap("balance");
    this.active = this.doc.getMap("active");
    this.sessions = this.doc.getMap("sessions");
    this.prizes = this.doc.getMap("prizes");
    this.prizeLog = this.doc.getArray("prize_log");

    // The state vector the server had at our last successful sync. We send
    // it on every push so the server can diff its missing changes for us.
    this.lastServerSV = null;

    this.status = new SyncStatusStore();
    // Tracks the most recent rejection (rule + message); SyncBanner pops a
    // toast for each new value of `rejection.id`.
    this.rejection = new SyncStatusStore();
    this.rejection.set(null);

    this._pushTimer = null;
    this._pollTimer = null;
    this._undoManager = new Y.UndoManager([this.balance, this.active, this.sessions, this.prizes, this.prizeLog], {
      // Only track *local* changes so a server-applied change doesn't get
      // un-done by the rejection rollback below.
      trackedOrigins: new Set([null, undefined]),
    });

    // y-indexeddb auto-persists every doc op into IDB. Once the persistence
    // layer has hydrated whatever was on disk, kick off the first sync.
    this.persistence = new IndexeddbPersistence(IDB_DB_NAME, this.doc);
    this.persistence.once("synced", () => {
      this._seedDefaultPrizesIfEmpty();
      this._startPolling();
      this._scheduleSync();
    });

    // Trigger a debounced push on every locally-originated mutation so the
    // server hears about user actions promptly. Updates we just applied
    // from the server tunnel through with origin=ORIGIN_REMOTE and skip.
    this.doc.on("update", (_update, origin) => {
      if (origin === ORIGIN_REMOTE) return;
      this._scheduleSync();
    });
  }

  /** Idempotent default prize seeding. The server seeds these too on first
   *  boot, so this only matters for the truly-offline first launch. */
  _seedDefaultPrizesIfEmpty() {
    if (this.prizes.size > 0) return;
    this.doc.transact(() => {
      for (const [id, name, cost] of DEFAULT_PRIZES) {
        const entry = new Y.Map();
        this.prizes.set(id, entry);
        entry.set("name", name);
        entry.set("cost", cost);
      }
    });
  }

  _startPolling() {
    if (this._pollTimer) return;
    this._pollTimer = setInterval(() => this.syncOnce(), POLL_INTERVAL_MS);
    // Fire one sync the moment the tab regains focus / visibility so a
    // newly-foregrounded device picks up other-device writes immediately.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") this._scheduleSync();
    });
  }

  _scheduleSync() {
    if (this._pushTimer) return;
    this._pushTimer = setTimeout(() => {
      this._pushTimer = null;
      this.syncOnce();
    }, PUSH_DEBOUNCE_MS);
  }

  /** Run one round-trip against /sync. Errors land in `this.status`. */
  async syncOnce() {
    this.status.set({ kind: "syncing" });
    const sv = this.lastServerSV ?? new Uint8Array();
    const update = Y.encodeStateAsUpdate(this.doc, sv);

    let response;
    try {
      response = await fetch(SYNC_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state_vector_b64: bytesToB64(Y.encodeStateVector(this.doc)),
          update_b64: bytesToB64(update),
        }),
      });
    } catch (e) {
      // Pure network error — DNS, TCP refused, CORS, offline.
      this.status.set({ kind: "offline", reason: e.message ?? String(e) });
      return;
    }

    if (response.status === 409) {
      // Server rejected our update. Roll back the last local transaction
      // via UndoManager so canonical state on this client matches the
      // server's view, then surface the rejection.
      //
      // A single push can carry multiple local transactions (queued
      // during the debounce window or accumulated while offline). The
      // server validates the merged result, so a single `undo()` may
      // leave invalid state behind — drain the entire local-origin
      // undo stack before resyncing so the next round is built on the
      // server's known-good view.
      let body;
      try {
        body = await response.json();
      } catch {
        body = { rejection: { rule: "malformed", message: response.statusText } };
      }
      while (this._undoManager.undoStack.length > 0) {
        this._undoManager.undo();
      }
      this.rejection.set({
        id: Date.now(),
        rule: body.rejection?.rule ?? "unknown",
        message: body.rejection?.message ?? "(no detail)",
      });
      this.status.set({
        kind: "rejected",
        rule: body.rejection?.rule ?? "unknown",
        message: body.rejection?.message ?? "",
      });
      // Force a pull-only sync so the local doc realigns with canonical
      // before the user retries the action.
      this._scheduleSync();
      return;
    }

    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const t = await response.text();
        if (t) detail += `: ${t.slice(0, 200)}`;
      } catch {
        /* fall through */
      }
      this.status.set({ kind: "offline", reason: detail });
      return;
    }

    let body;
    try {
      body = await response.json();
    } catch (e) {
      this.status.set({ kind: "offline", reason: `bad JSON from /sync: ${e.message}` });
      return;
    }

    const serverUpdate = b64ToBytes(body.update_b64);
    if (serverUpdate.byteLength > 0) {
      // Apply with a remote origin tag so our own update listener doesn't
      // bounce it back at the server.
      Y.applyUpdate(this.doc, serverUpdate, ORIGIN_REMOTE);
    }
    this.lastServerSV = b64ToBytes(body.state_vector_b64);
    this.status.set({ kind: "ok", lastSyncedAt: Date.now() });
  }

  /** Run a transaction. All UI mutations go through here so the UndoManager
   *  groups the change set, which is what the rejection rollback undoes. */
  mutate(fn) {
    this.doc.transact(fn);
  }
}

export const casinoSync = new CasinoSync();

// Re-export Y bits used by the UI (constructing nested Y.Map for sessions/
// prizes happens in study_casino.jsx).
export { Y };
