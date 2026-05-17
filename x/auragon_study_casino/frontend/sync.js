// REST + thin WebSocket client for the Study Casino.
//
// Architecture: the server holds canonical state in Postgres tables. The client
// keeps an in-memory cache of the last `GET /state` response, refetched after
// every successful action this tab posts, and again whenever the server sends
// a `{"type":"state_changed"}` ping over `/ws` (which fires after any other
// tab — or any other device — successfully posts an action).
//
// State shape (mirrors the server's `state_dump` / `state_snapshots.decoded_json`):
//
//   {
//     balance:   { credits: int, tokens: int },
//     sessions:  [{ id, subject, seconds, ended_at_ms }],
//     prizes:    [{ id, name, cost }],
//     prize_log: [{ id, name, cost, at_ms }],
//   }
//
// In-progress (active) study-session timer state lives in `localStorage`,
// not on the server — see `use_casino.js`. Only `/actions/session/complete`
// and `/actions/session/add-past` ever insert into the `sessions` table.

import { useEffect, useState } from "react";

const WS_RECONNECT_BASE_MS = 1_000;
const WS_RECONNECT_MAX_MS = 30_000;

class Observable {
  constructor(initial) {
    this.value = initial;
    this.listeners = new Set();
  }
  set(value) {
    this.value = value;
    for (const l of this.listeners) l(value);
  }
  subscribe(listener) {
    this.listeners.add(listener);
    listener(this.value);
    return () => this.listeners.delete(listener);
  }
}

class CasinoSync {
  constructor() {
    this.state = new Observable(null);
    this.status = new Observable({ kind: "syncing" });
    // SyncIcon pops a toast for each new value of `rejection.id`.
    this.rejection = new Observable(null);
    // { username, is_admin } once /me returns. null until then.
    this.me = new Observable(null);

    this._ws = null;
    this._reconnectTimer = null;
    this._reconnectDelay = WS_RECONNECT_BASE_MS;

    this._init();
  }

  async _init() {
    try {
      const resp = await fetch("/me", { credentials: "same-origin" });
      if (resp.status === 401) {
        window.location.href = "/auth/login";
        return;
      }
      if (resp.ok) {
        this.me.set(await resp.json());
      }
    } catch {
      // /me is best-effort; if it fails we still try /state and /ws.
    }
    await this.fetchState();
    this._connectWebSocket();

    // Re-sync when a backgrounded tab comes back to the foreground.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      this.fetchState();
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
        if (!this._reconnectTimer) this._connectWebSocket();
      }
    });
  }

  /** Re-fetch /state and update the cache. Called automatically after every
   *  postAction() and on every WS `state_changed` ping; exposed publicly so
   *  SyncIcon can offer a manual "retry" affordance. */
  async syncOnce() {
    await this.fetchState();
  }

  async fetchState() {
    this.status.set({ kind: "syncing" });
    try {
      const resp = await fetch("/state", { credentials: "same-origin" });
      if (resp.status === 401) {
        window.location.href = "/auth/login";
        return;
      }
      if (!resp.ok) {
        this.status.set({ kind: "offline", reason: `state fetch failed: ${resp.status}` });
        return;
      }
      const body = await resp.json();
      this.state.set(body);
      this.status.set({ kind: "ok", lastSyncedAt: Date.now() });
    } catch (e) {
      this.status.set({ kind: "offline", reason: `state fetch failed: ${e.message ?? e}` });
    }
  }

  /** POST a server action; refetch state on success; surface 409 rule
   *  rejections through `rejection` (so SyncIcon's toast pops) and 4xx/5xx
   *  through `status` (so the banner reflects the network state). Returns
   *  the parsed JSON body on success, or throws Error on failure. */
  async postAction(path, body) {
    this.status.set({ kind: "syncing" });
    let resp;
    try {
      resp = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(body),
      });
    } catch (e) {
      this.status.set({ kind: "offline", reason: `${e.message ?? e}` });
      throw e;
    }

    if (resp.status === 401) {
      window.location.href = "/auth/login";
      throw new Error("not authenticated");
    }

    if (resp.status === 409) {
      const detail = (await resp.json()).detail ?? {};
      const rule = detail.rule ?? "unknown";
      const message = detail.message ?? "rejected";
      this.rejection.set({ id: Date.now(), rule, message });
      this.status.set({ kind: "rejected", rule, message });
      // Refetch so optimistic UI converges back on the truth.
      this.fetchState();
      throw new Error(message);
    }

    if (!resp.ok) {
      const text = await resp.text().catch(() => `${resp.status}`);
      this.status.set({ kind: "offline", reason: text });
      throw new Error(text);
    }

    const result = await resp.json();
    // Refetch — server's WS will also send state_changed but the round-trip
    // is faster if we trigger it here too. fetchState updates `status`.
    this.fetchState();
    return result;
  }

  _connectWebSocket() {
    if (this._ws) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    const ws = new WebSocket(url);
    this._ws = ws;

    ws.onopen = () => {
      this._reconnectDelay = WS_RECONNECT_BASE_MS;
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "state_changed") {
          this.fetchState();
        }
      } catch {
        // ignore malformed WS frames
      }
    };

    ws.onclose = (event) => {
      this._ws = null;
      if (event.code === 4001) {
        window.location.href = "/auth/login";
        return;
      }
      this._scheduleReconnect();
    };

    ws.onerror = () => {
      // Error details are not exposed by the WS spec; close event follows.
    };
  }

  _scheduleReconnect() {
    if (this._reconnectTimer) return;
    const delay = this._reconnectDelay;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._reconnectDelay = Math.min(this._reconnectDelay * 2, WS_RECONNECT_MAX_MS);
      this._connectWebSocket();
    }, delay);
  }
}

export const casinoSync = new CasinoSync();

/** Subscribe to the current state cache. Returns null until the first
 *  /state fetch completes. */
export function useCasinoState() {
  const [state, setState] = useState(casinoSync.state.value);
  useEffect(() => casinoSync.state.subscribe(setState), []);
  return state;
}

/** Subscribe to the global SyncStatus store. */
export function useSyncStatus() {
  const [state, setState] = useState(casinoSync.status.value);
  useEffect(() => casinoSync.status.subscribe(setState), []);
  return state;
}

/** Subscribe to the latest sync rejection (or null). */
export function useSyncRejection() {
  const [state, setState] = useState(casinoSync.rejection.value);
  useEffect(() => casinoSync.rejection.subscribe(setState), []);
  return state;
}

/** Subscribe to the {username, is_admin} payload returned by /me.
 *  Returns null until the initial /me fetch completes. */
export function useMe() {
  const [state, setState] = useState(casinoSync.me.value);
  useEffect(() => casinoSync.me.subscribe(setState), []);
  return state;
}

async function adminFetch(path) {
  const resp = await fetch(path, { credentials: "same-origin" });
  if (resp.status === 401) {
    // Mirror the auth-expiry behaviour of fetchState/postAction so an
    // expired session doesn't leave the admin panel showing a raw 401.
    window.location.href = "/auth/login";
    throw new Error("not authenticated");
  }
  if (!resp.ok) throw new Error(`${path} ${resp.status}`);
  return resp.json();
}

/** Admin-only: fetch the list of usernames the server has seeded. */
export async function fetchAdminUsers() {
  return (await adminFetch("/admin/users")).users;
}

/** Admin-only: fetch the state_dump for any user. */
export async function fetchAdminUserState(username) {
  return adminFetch(`/admin/state?user=${encodeURIComponent(username)}`);
}
