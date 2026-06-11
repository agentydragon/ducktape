/**
 * REST API client for the Airlock operator frontend.
 *
 * Communicates with the operator-facing REST API at /api.
 * Auth is handled by an OAuth2 JWT (Authorization Code + PKCE flow).
 * Real-time updates are received via SSE from /api/events.
 */
import { getAccessToken } from "./auth.ts";
import type { Action, ActionKey, ActionStatus, BackendStatus, DeploymentInfo, OAuthProviderStatus } from "./types.ts";

type Callback<T> = (data: T) => void;

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await getAccessToken();
  const resp = await fetch(path, {
    ...init,
    headers: {
      ...init?.headers,
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`API error ${resp.status}: ${text}`);
  }
  return resp.json();
}

export class AirlockApiClient {
  private listChangedCallbacks = new Set<Callback<void>>();
  private actionCallbacks = new Map<string, Set<Callback<unknown>>>();
  private backendsChangedCallbacks = new Set<Callback<void>>();
  private eventSource: EventSource | null = null;

  static async connect(): Promise<AirlockApiClient> {
    const client = new AirlockApiClient();
    await client.connectSSE();
    return client;
  }

  private async connectSSE(): Promise<void> {
    const token = await getAccessToken();
    // EventSource doesn't support custom headers, so we use fetch-based SSE.
    const resp = await fetch("/api/events", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok || !resp.body) return;

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const processEvents = async () => {
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";
          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const event = JSON.parse(line.slice(6));
                this.handleEvent(event);
              } catch {
                // Ignore malformed SSE data lines.
              }
            }
          }
        }
      } catch {
        // Reconnect on error after a delay.
        setTimeout(() => this.connectSSE(), 3000);
      }
    };
    processEvents(); // Don't await — runs in background
  }

  private handleEvent(event: { type: string; session_key?: string; action_seq?: number }): void {
    if (event.type === "actions_changed") {
      for (const cb of this.listChangedCallbacks) cb(undefined as unknown as void);
    } else if (event.type === "action_updated" && event.session_key && event.action_seq != null) {
      const uri = `${event.session_key}/${event.action_seq}`;
      const cbs = this.actionCallbacks.get(uri);
      if (cbs) {
        this.getAction(event.session_key, event.action_seq).then((action) => {
          for (const cb of cbs) (cb as Callback<Action>)(action);
        });
      }
    } else if (event.type === "backends_changed") {
      for (const cb of this.backendsChangedCallbacks) cb(undefined as unknown as void);
    }
  }

  /** Subscribe to action list changes (new actions arriving). */
  onListChanged(cb: Callback<void>): () => void {
    this.listChangedCallbacks.add(cb);
    return () => this.listChangedCallbacks.delete(cb);
  }

  /** Subscribe to backend status changes. */
  onBackendsChanged(cb: Callback<void>): () => void {
    this.backendsChangedCallbacks.add(cb);
    return () => this.backendsChangedCallbacks.delete(cb);
  }

  /** Subscribe to updates for a specific action. Fires the callback with the initial state immediately. */
  async subscribeAction<T>(sessionKey: string, actionSeq: number, cb: Callback<T>): Promise<() => void> {
    const uri = `${sessionKey}/${actionSeq}`;
    if (!this.actionCallbacks.has(uri)) {
      this.actionCallbacks.set(uri, new Set());
    }
    this.actionCallbacks.get(uri)!.add(cb as Callback<unknown>);

    // Initial read
    try {
      const action = await this.getAction(sessionKey, actionSeq);
      (cb as Callback<Action>)(action);
    } catch (e) {
      console.error(`Initial read failed for ${uri}:`, e);
    }

    return () => {
      const cbs = this.actionCallbacks.get(uri);
      if (!cbs) return;
      cbs.delete(cb as Callback<unknown>);
      if (cbs.size === 0) this.actionCallbacks.delete(uri);
    };
  }

  async listActions(status?: ActionStatus, limit = 100, offset = 0): Promise<Action[]> {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    return apiFetch<Action[]>(`/api/actions?${params}`);
  }

  async getAction(sessionKey: string, actionSeq: number): Promise<Action> {
    return apiFetch<Action>(`/api/actions/${sessionKey}/${actionSeq}`);
  }

  async approve(key: ActionKey): Promise<void> {
    await apiFetch<unknown>(`/api/actions/${key.session_key}/${key.action_seq}/approve`, {
      method: "POST",
    });
  }

  async reject(key: ActionKey, reason?: string): Promise<void> {
    await apiFetch<unknown>(`/api/actions/${key.session_key}/${key.action_seq}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    });
  }

  async listOAuthProviders(): Promise<OAuthProviderStatus[]> {
    return apiFetch<OAuthProviderStatus[]>("/api/oauth/providers");
  }

  async listBackends(): Promise<BackendStatus[]> {
    return apiFetch<BackendStatus[]>("/api/backends");
  }

  async getDeploymentInfo(): Promise<DeploymentInfo> {
    return apiFetch<DeploymentInfo>("/api/info");
  }
}

let _clientPromise: Promise<AirlockApiClient> | null = null;

/** Lazily connect to the API; returns the same instance on repeated calls. */
export function getApiClient(): Promise<AirlockApiClient> {
  if (!_clientPromise) _clientPromise = AirlockApiClient.connect();
  return _clientPromise;
}
