// CRDT-backed multi-device sync for the Study Casino.
//
// Architecture: one Y.Doc per running tab, persisted to IndexedDB by
// `y-indexeddb`, synced to the server via a persistent WebSocket at /ws.
// The server speaks the same Yrs/Yjs binary update format and gates writes
// through the Python validators in validators.py; this module is the
// wire-format mirror.
//
// The doc shape is documented in `x/auragon_study_casino/doc_shape.py` —
// keep both in lockstep when adding fields:
//
//   doc.getMap("balance")    : { credits: number, tokens: number }
//   doc.getMap("sessions")   : id -> Y.Map — all sessions, in-progress or done.
//                              In-progress: { subject, start_time_ms, paused,
//                                            paused_duration_ms, pause_started_at_ms }
//                              Completed:   { subject, seconds, ended_at_ms }
//   doc.getMap("prizes")     : id -> Y.Map({ name, cost })
//   doc.getArray("prize_log"): Y.Map({ id, name, cost, at_ms })
//   doc.getMap("active")     : legacy map — kept for one-time migration only
//
// WebSocket protocol (JSON, both directions):
//   client → server: { type:"sync", state_vector_b64, update_b64 }
//   server → client: { type:"accepted", update_b64, state_vector_b64 }
//                  | { type:"rejected", rule, message }
//                  | { type:"server_push", update_b64 }   ← another tab synced
//                  | { type:"error", code, message }
//
// Error policy: the frontend never silently swallows sync failures. Every
// network or validation error surfaces through `casinoSync.status`; the
// SyncIcon in the header renders that store.

import * as Y from "yjs";
import { IndexeddbPersistence } from "y-indexeddb";

const PUSH_DEBOUNCE_MS = 200;
const WS_RECONNECT_BASE_MS = 1_000;
const WS_RECONNECT_MAX_MS = 30_000;
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
    this.sessions = this.doc.getMap("sessions");
    this.prizes = this.doc.getMap("prizes");
    this.prizeLog = this.doc.getArray("prize_log");
    // Legacy map kept only for one-time migration of old in-progress sessions.
    this.active = this.doc.getMap("active");

    // The state vector the server had at our last successful sync. We send
    // it on every push so the server can diff its missing changes for us.
    this.lastServerSV = null;

    this.status = new SyncStatusStore();
    // Tracks the most recent rejection (rule + message); SyncIcon pops a
    // toast for each new value of `rejection.id`.
    this.rejection = new SyncStatusStore();
    this.rejection.set(null);

    this._pushTimer = null;
    this._ws = null;
    this._wsReady = false;
    this._reconnectTimer = null;
    this._reconnectDelay = WS_RECONNECT_BASE_MS;

    // UndoManager tracks balance, sessions, prizes, and prize_log but NOT
    // the legacy active map. Only local-origin changes are tracked so server
    // updates don't enter the undo stack.
    this._undoManager = new Y.UndoManager([this.balance, this.sessions, this.prizes, this.prizeLog], {
      trackedOrigins: new Set([null, undefined]),
    });

    // Fetch the current user from /me, then open the per-user IDB namespace
    // and start syncing. A 401 means the session expired — redirect to login.
    this._init();

    // Trigger a debounced push on every locally-originated mutation so the
    // server hears about user actions promptly. Updates we just applied
    // from the server tunnel through with origin=ORIGIN_REMOTE and skip.
    this.doc.on("update", (_update, origin) => {
      if (origin === ORIGIN_REMOTE) return;
      console.debug("[CasinoSync] local mutation detected, scheduling sync");
      this._scheduleSync();
    });
  }

  async _init() {
    let username = "default";
    try {
      const resp = await fetch("/me", { credentials: "same-origin" });
      if (resp.status === 401) {
        window.location.href = "/auth/login";
        return;
      }
      if (resp.ok) {
        const data = await resp.json();
        username = data.username || "default";
        console.debug("[CasinoSync] authenticated as", username);
      }
    } catch (e) {
      // Network error during /me — proceed with "default"; WS will fail
      // too and surface the offline state.
      console.debug("[CasinoSync] /me fetch failed:", e.message);
    }

    const idbName = `casino-doc-v1-${username}`;
    console.debug("[CasinoSync] opening IDB:", idbName);
    this.persistence = new IndexeddbPersistence(idbName, this.doc);
    this.persistence.once("synced", () => {
      console.debug("[CasinoSync] IDB synced, seeding and connecting WS");
      this._seedDefaultPrizesIfEmpty();
      this._migrateActiveSessions();
      this._connectWebSocket();
    });
    // Fallback: if IDB hasn't fired "synced" within 3 s (e.g. headless
    // Chrome in a container), connect anyway.
    setTimeout(() => {
      if (!this._ws) {
        console.debug("[CasinoSync] IDB synced timeout — connecting WS anyway");
        this._connectWebSocket();
      }
    }, 3000);
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

  // CLEANUP(2026-04-28): Remove once all clients have migrated. Moves any
  // leftover session data from the old `active` Y.Map into the `sessions`
  // map as an in-progress entry (no ended_at_ms).
  _migrateActiveSessions() {
    if (this.active.size === 0) return;
    const hasInProgress = [...this.sessions.values()].some((m) => !m.get("ended_at_ms"));
    if (hasInProgress) {
      // New-style active session already present; just clear the legacy map.
      console.debug("[CasinoSync] migration: legacy active map has data but new-style active exists, clearing legacy");
      this.doc.transact(() => this.active.clear());
      return;
    }
    const subject = this.active.get("subject");
    if (!subject) {
      this.doc.transact(() => this.active.clear());
      return;
    }
    const id = `migrated-${Date.now()}`;
    console.debug("[CasinoSync] migrating legacy active session to sessions:", { subject, id });
    this.doc.transact(() => {
      const sm = new Y.Map();
      this.sessions.set(id, sm);
      sm.set("subject", subject);
      sm.set("start_time_ms", this.active.get("start_time_ms") ?? Date.now());
      sm.set("paused", !!this.active.get("paused"));
      sm.set("paused_duration_ms", this.active.get("paused_duration_ms") ?? 0);
      sm.set("pause_started_at_ms", this.active.get("pause_started_at_ms") ?? null);
      this.active.clear();
    });
  }

  _connectWebSocket() {
    if (this._ws) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    console.debug("[CasinoSync] connecting WebSocket:", url);

    const ws = new WebSocket(url);
    this._ws = ws;

    ws.onopen = () => {
      console.debug("[CasinoSync] WebSocket connected");
      this._wsReady = true;
      this._reconnectDelay = WS_RECONNECT_BASE_MS;
      this.status.set({ kind: "syncing" });
      // Server sends a full bootstrap snapshot on connect; no need to push here
      // unless there are local-only changes the server hasn't seen yet.
      this._scheduleSync();
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this._handleMessage(msg);
      } catch (e) {
        console.debug("[CasinoSync] failed to parse message:", e.message);
      }
    };

    ws.onclose = (event) => {
      console.debug("[CasinoSync] WebSocket closed:", event.code, event.reason || "(no reason)");
      this._ws = null;
      this._wsReady = false;
      if (event.code === 4001) {
        // Auth failure — redirect rather than reconnect.
        window.location.href = "/auth/login";
        return;
      }
      this.status.set({ kind: "offline", reason: "disconnected, reconnecting…" });
      this._scheduleReconnect();
    };

    ws.onerror = () => {
      // Error details are not exposed by the WS spec; the close event follows.
      console.debug("[CasinoSync] WebSocket error (close follows)");
    };

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (this._wsReady) {
        console.debug("[CasinoSync] tab foregrounded, syncing");
        this._scheduleSync();
      } else if (!this._reconnectTimer) {
        console.debug("[CasinoSync] tab foregrounded, reconnecting");
        this._connectWebSocket();
      }
    });
  }

  _scheduleReconnect() {
    if (this._reconnectTimer) return;
    const delay = this._reconnectDelay;
    console.debug("[CasinoSync] scheduling reconnect in", delay, "ms");
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._reconnectDelay = Math.min(this._reconnectDelay * 2, WS_RECONNECT_MAX_MS);
      this._connectWebSocket();
    }, delay);
  }

  _scheduleSync() {
    if (this._pushTimer) return;
    this._pushTimer = setTimeout(() => {
      this._pushTimer = null;
      this._doSync();
    }, PUSH_DEBOUNCE_MS);
  }

  _doSync() {
    if (!this._wsReady || !this._ws) {
      console.debug("[CasinoSync] sync skipped — WebSocket not ready");
      return;
    }
    const update = Y.encodeStateAsUpdate(this.doc, this.lastServerSV?.byteLength ? this.lastServerSV : undefined);
    const sv = Y.encodeStateVector(this.doc);
    console.debug("[CasinoSync] sending sync — update:", update.byteLength, "B, sv:", sv.byteLength, "B");
    this.status.set({ kind: "syncing" });
    this._ws.send(
      JSON.stringify({
        type: "sync",
        state_vector_b64: bytesToB64(sv),
        update_b64: bytesToB64(update),
      })
    );
  }

  _handleMessage(msg) {
    console.debug("[CasinoSync] received:", msg.type);

    if (msg.type === "accepted") {
      const serverUpdate = b64ToBytes(msg.update_b64);
      if (serverUpdate.byteLength > 0) {
        try {
          Y.applyUpdate(this.doc, serverUpdate, ORIGIN_REMOTE);
          console.debug("[CasinoSync] applied server update:", serverUpdate.byteLength, "B");
        } catch (e) {
          console.debug("[CasinoSync] corrupt server update:", e.message);
          this.status.set({ kind: "offline", reason: `corrupt server update: ${e.message ?? String(e)}` });
          return;
        }
      }
      const sv = b64ToBytes(msg.state_vector_b64);
      this.lastServerSV = sv.byteLength > 0 ? sv : null;
      // Clear the undo stack so changes already on the server can't be
      // rolled back by a future 409 rejection of unrelated local edits.
      this._undoManager.clear();
      this.status.set({ kind: "ok", lastSyncedAt: Date.now() });
    }

    if (msg.type === "rejected") {
      console.debug("[CasinoSync] sync rejected:", msg.rule, msg.message);
      // Drain undo stack: only contains changes since the last successful
      // sync (the clear() above keeps the stack bounded).
      while (this._undoManager.undoStack.length > 0) {
        this._undoManager.undo();
      }
      this.rejection.set({ id: Date.now(), rule: msg.rule, message: msg.message });
      this.status.set({ kind: "rejected", rule: msg.rule, message: msg.message });
      // Pull from server to realign before the user retries.
      setTimeout(() => this._doSync(), 100);
    }

    if (msg.type === "server_push") {
      const serverUpdate = b64ToBytes(msg.update_b64);
      if (serverUpdate.byteLength > 0) {
        try {
          Y.applyUpdate(this.doc, serverUpdate, ORIGIN_REMOTE);
          console.debug("[CasinoSync] applied server push:", serverUpdate.byteLength, "B");
        } catch (e) {
          console.debug("[CasinoSync] server push error:", e.message);
        }
      }
    }

    if (msg.type === "error") {
      console.debug("[CasinoSync] server error:", msg.code, msg.message);
      if (msg.code === 401) {
        window.location.href = "/auth/login";
      }
    }
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
