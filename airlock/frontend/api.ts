/**
 * REST API client for the Airlock operator frontend.
 *
 * Replaces the former MCP-based operator client. Uses fetch for REST calls
 * and fetch-based SSE streaming for live updates from /api/events (using
 * fetch + ReadableStream instead of EventSource to support Bearer auth headers).
 */
import { getAccessToken } from "./auth.ts";
import type { Action, ActionKey, ActionStatus } from "./types.ts";

type Callback<T> = (data: T) => void;

export class AirlockApiClient {
  private token: string;
  private sseAbort: AbortController | null = null;
  private actionCallbacks = new Map<string, Set<Callback<Action>>>();
  private listChangedCallbacks = new Set<Callback<void>>();

  private constructor(token: string) {
    this.token = token;
  }

  static async connect(): Promise<AirlockApiClient> {
    const token = await getAccessToken();
    const client = new AirlockApiClient(token);
    client.connectSSE();
    return client;
  }

  private connectSSE(): void {
    this.sseAbort = new AbortController();
    const processStream = async () => {
      const resp = await fetch("/api/events", {
        headers: { Authorization: `Bearer ${this.token}` },
        signal: this.sseAbort!.signal,
      });
      if (!resp.ok || !resp.body) return;
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop()!;
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const data = JSON.parse(line.slice(6)) as { action?: Action };
            for (const cb of this.listChangedCallbacks) cb(undefined as unknown as void);
            if (data.action) {
              const key = `${data.action.key.session_key}/${data.action.key.action_seq}`;
              const cbs = this.actionCallbacks.get(key);
              if (cbs) for (const cb of cbs) cb(data.action);
            }
          } catch {
            // Ignore keepalive or malformed events
          }
        }
      }
    };
    processStream().catch(() => {
      // Stream ended or aborted — could reconnect here
    });
  }

  private async fetch(path: string, init?: RequestInit): Promise<Response> {
    return fetch(path, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.token}`,
        ...init?.headers,
      },
    });
  }

  /** Subscribe to list change events (new actions arriving). */
  onListChanged(cb: Callback<void>): () => void {
    this.listChangedCallbacks.add(cb);
    return () => this.listChangedCallbacks.delete(cb);
  }

  /** Subscribe to updates for a specific action. Returns unsubscribe fn. */
  subscribeAction(key: ActionKey, cb: Callback<Action>): () => void {
    const id = `${key.session_key}/${key.action_seq}`;
    if (!this.actionCallbacks.has(id)) {
      this.actionCallbacks.set(id, new Set());
    }
    this.actionCallbacks.get(id)!.add(cb);
    return () => {
      const cbs = this.actionCallbacks.get(id);
      if (!cbs) return;
      cbs.delete(cb);
      if (cbs.size === 0) this.actionCallbacks.delete(id);
    };
  }

  async listActions(status?: ActionStatus, limit = 100, offset = 0): Promise<Action[]> {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    const resp = await this.fetch(`/api/actions?${params}`);
    if (!resp.ok) throw new Error(`listActions failed: ${resp.status}`);
    return (await resp.json()) as Action[];
  }

  async getAction(key: ActionKey): Promise<Action> {
    const resp = await this.fetch(`/api/actions/${key.session_key}/${key.action_seq}`);
    if (!resp.ok) throw new Error(`getAction failed: ${resp.status}`);
    return (await resp.json()) as Action;
  }

  async approve(key: ActionKey): Promise<void> {
    const resp = await this.fetch(`/api/actions/${key.session_key}/${key.action_seq}/approve`, {
      method: "POST",
    });
    if (!resp.ok) throw new Error(`approve failed: ${resp.status}`);
  }

  async reject(key: ActionKey, reason?: string): Promise<void> {
    const resp = await this.fetch(`/api/actions/${key.session_key}/${key.action_seq}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason ?? null }),
    });
    if (!resp.ok) throw new Error(`reject failed: ${resp.status}`);
  }
}

let _clientPromise: Promise<AirlockApiClient> | null = null;

/** Lazily connect to the REST API; returns the same instance on repeated calls. */
export function getApiClient(): Promise<AirlockApiClient> {
  if (!_clientPromise) _clientPromise = AirlockApiClient.connect();
  return _clientPromise;
}
