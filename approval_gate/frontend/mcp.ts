/**
 * MCP client for the approval gate operator frontend.
 *
 * Connects to the operator-facing MCP endpoint at /mcp (port 8765).
 * Auth is handled by the Authentik session cookie — no bearer token needed.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import {
  ResourceListChangedNotificationSchema,
  ResourceUpdatedNotificationSchema,
  type ResourceUpdatedNotification,
} from "@modelcontextprotocol/sdk/types.js";
import type { Action, ActionKey, ActionStatus } from "./types.ts";

type Callback<T> = (data: T) => void;

export class ApprovalGateMcpClient {
  private client: Client;
  private subscriptions = new Map<string, Set<Callback<unknown>>>();
  private listChangedCallbacks = new Set<Callback<void>>();

  private constructor(client: Client) {
    this.client = client;
    this.client.setNotificationHandler(
      ResourceUpdatedNotificationSchema,
      async (notification: ResourceUpdatedNotification) => {
        const uri = notification.params?.uri;
        if (uri) await this.notifyResourceSubscribers(uri);
      }
    );
    this.client.setNotificationHandler(ResourceListChangedNotificationSchema, async () => {
      for (const cb of this.listChangedCallbacks) cb(undefined as unknown as void);
    });
  }

  static async connect(): Promise<ApprovalGateMcpClient> {
    const transport = new StreamableHTTPClientTransport(new URL("/mcp", window.location.origin));
    const client = new Client({ name: "approval-gate-ui", version: "1.0.0" }, { capabilities: {} });
    await client.connect(transport);
    return new ApprovalGateMcpClient(client);
  }

  /** Subscribe to resource list changes (new actions arriving). */
  onListChanged(cb: Callback<void>): () => void {
    this.listChangedCallbacks.add(cb);
    return () => this.listChangedCallbacks.delete(cb);
  }

  /** Subscribe to updates for a specific action resource URI. */
  async subscribeAction<T>(uri: string, cb: Callback<T>): Promise<() => void> {
    if (!this.subscriptions.has(uri)) {
      this.subscriptions.set(uri, new Set());
      await this.client.subscribeResource({ uri });
    }
    this.subscriptions.get(uri)!.add(cb as Callback<unknown>);

    // Initial read
    try {
      const data = await this.readAction<T>(uri);
      cb(data);
    } catch (e) {
      console.error(`Initial read failed for ${uri}:`, e);
    }

    return () => {
      const cbs = this.subscriptions.get(uri);
      if (!cbs) return;
      cbs.delete(cb as Callback<unknown>);
      if (cbs.size === 0) {
        this.subscriptions.delete(uri);
        this.client.unsubscribeResource({ uri }).catch((e: unknown) => {
          console.warn(`Failed to unsubscribe ${uri}:`, e);
        });
      }
    };
  }

  async callTool<T>(name: string, args: Record<string, unknown>): Promise<T> {
    const result = await this.client.callTool({ name, arguments: args });
    const first = result.content[0];
    if (!first || first.type !== "text") throw new Error(`No text content from tool ${name}`);
    return JSON.parse(first.text as string) as T;
  }

  async listActions(status?: ActionStatus, limit = 100, offset = 0): Promise<Action[]> {
    return this.callTool("list_actions", { status: status ?? null, limit, offset });
  }

  async approve(key: ActionKey): Promise<Action> {
    return this.callTool("approve_action", { key });
  }

  async reject(key: ActionKey, reason?: string): Promise<Action> {
    return this.callTool("reject_action", { key, reason: reason ?? null });
  }

  async readAction<T>(uri: string): Promise<T> {
    const result = await this.client.readResource({ uri });
    const first = result.contents[0];
    if (!first || !("text" in first)) throw new Error(`No text content for ${uri}`);
    return JSON.parse(first.text as string) as T;
  }

  private async notifyResourceSubscribers(uri: string): Promise<void> {
    const cbs = this.subscriptions.get(uri);
    if (!cbs || cbs.size === 0) return;
    try {
      const data = await this.readAction(uri);
      for (const cb of cbs) cb(data);
    } catch (e) {
      console.error(`Failed to read resource ${uri}:`, e);
    }
  }
}

let _clientPromise: Promise<ApprovalGateMcpClient> | null = null;

/** Lazily connect to the MCP endpoint; returns the same instance on repeated calls. */
export function getMcpClient(): Promise<ApprovalGateMcpClient> {
  if (!_clientPromise) _clientPromise = ApprovalGateMcpClient.connect();
  return _clientPromise;
}
