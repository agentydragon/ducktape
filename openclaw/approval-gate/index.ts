/**
 * Approval Gate OpenClaw plugin.
 *
 * Connects to the approval gate MCP server as a persistent client and:
 *   1. Discovers approval-gate tools via MCP list_tools.
 *   2. Re-registers each tool with OpenClaw, stripping `session_key` from the
 *      schema (injected automatically from ctx.sessionKey).
 *   3. Subscribes to `resource://actions/{id}` MCP resource notifications.
 *   4. On ResourceUpdated: reads the resource, formats a result message, and
 *      injects it into the agent session via chat.inject (local gateway WebSocket).
 *
 * Resilience: if the approval gate MCP server goes down, the plugin automatically
 * reconnects with exponential backoff. On reconnect, it re-subscribes to all
 * pending (non-terminal) actions and delivers any results that arrived during the
 * outage via catch-up reads.
 *
 * Auth:
 *   - Approval gate MCP endpoint: Bearer AGENT_API_KEY (from plugin config)
 *   - chat.inject call: OPENCLAW_GATEWAY_TOKEN (env var, same process)
 *
 * The approval gate server itself never holds the OpenClaw gateway token.
 */

import type { OpenClawPluginApi, OpenClawPluginToolContext } from "openclaw/plugin-sdk";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import WebSocket from "ws";

const DEFAULT_GATEWAY_WS_URL = "ws://127.0.0.1:18789";
const ACTION_RESOURCE_PREFIX = "resource://actions/";
const INITIAL_RETRY_DELAY_MS = 5_000;
const MAX_RETRY_DELAY_MS = 60_000;

// ── Action state types (mirrors approval_gate/models.py + mcp_types.CallToolResult) ─
// TODO: These types overlap with approval_gate/frontend/types.ts. The two packages live in
// different environments (Node.js plugin vs browser SPA) and carry different field sets, so
// a shared package would add more complexity than it removes for now. If a third consumer
// appears, consider extracting a shared @ducktape/approval-gate-types workspace package.

interface ActionState {
  status: "pending" | "executing" | "done" | "rejected" | "withdrawn";
}

interface DoneState extends ActionState {
  status: "done";
  outcome: {
    content: Array<{ type: string; text?: string; [key: string]: unknown }>;
    isError?: boolean | null;
  };
}

interface RejectedState extends ActionState {
  status: "rejected";
  reason?: string | null;
}

interface Action {
  id: string;
  call: { tool_name: string };
  session_key?: string | null;
  state: ActionState;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

type TerminalStatus = "done" | "rejected" | "withdrawn";
const TERMINAL_STATUSES = new Set<TerminalStatus>(["done", "rejected", "withdrawn"]);

function isTerminal(status: string): status is TerminalStatus {
  return TERMINAL_STATUSES.has(status as TerminalStatus);
}

function formatOutcomeMessage(action: Action): string {
  const state = action.state;
  const id = action.id;

  if (state.status === "done") {
    const done = state as DoneState;
    const parts = done.outcome.content.filter((c) => c.type === "text" && c.text).map((c) => c.text as string);
    const body = parts.join("\n") || JSON.stringify(done.outcome.content, null, 2);
    if (!done.outcome.isError) {
      return `Action ${id} approved and executed:\n\n${body}`;
    } else {
      return `Action ${id} was approved but execution returned an error:\n\n${body}`;
    }
  }
  if (state.status === "rejected") {
    const rej = state as RejectedState;
    return `Action ${id} was rejected by the operator. Reason: ${rej.reason ?? "none given"}`;
  }
  if (state.status === "withdrawn") {
    return `Action ${id} was withdrawn.`;
  }
  return `Action ${id} state changed to: ${state.status}`;
}

/** Strip session_key from a JSON schema properties object. */
function stripSessionKey(schema: Record<string, unknown>): Record<string, unknown> {
  const result = structuredClone(schema) as Record<string, unknown>;
  const props = result.properties as Record<string, unknown> | undefined;
  if (props) {
    delete props.session_key;
  }
  const required = result.required as string[] | undefined;
  if (required) {
    result.required = required.filter((k) => k !== "session_key");
  }
  return result;
}

// ── Pending action tracking ─────────────────────────────────────────────────

type PendingAction = { resourceUri: string; sessionKey: string | null };

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type NotificationHandler = (notification: any) => Promise<void>;

// ── ApprovalGateConnection ──────────────────────────────────────────────────

/**
 * Resilient MCP client connection to the approval gate server.
 *
 * Automatically reconnects on disconnect with exponential backoff. Tracks
 * in-flight actions and re-subscribes on reconnect, performing catch-up reads
 * to deliver results that arrived during the outage.
 */
class ApprovalGateConnection {
  private client: Client | null = null;
  private connecting = false;
  private retryDelay = INITIAL_RETRY_DELAY_MS;
  private notificationHandler: NotificationHandler | null = null;
  private cachedInstructions: string | undefined;
  /** Actions that haven't reached terminal state — re-subscribed on reconnect. */
  private readonly pendingActions = new Map<string, PendingAction>();

  constructor(
    private readonly url: string,
    private readonly agentApiKey: string,
    private readonly log: ScopedLogger
  ) {}

  async connect(): Promise<void> {
    if (this.connecting) return;
    this.connecting = true;
    try {
      const transport = new StreamableHTTPClientTransport(new URL(this.url), {
        requestInit: {
          headers: { Authorization: `Bearer ${this.agentApiKey}` },
        },
      });
      const client = new Client({ name: "openclaw-approval-gate-plugin", version: "0.1.0" });

      client.onclose = () => {
        this.log.warn("MCP connection closed");
        this.client = null;
        this.scheduleReconnect();
      };

      client.onerror = (error: Error) => {
        this.log.warn(`MCP client error: ${error.message}`);
      };

      await client.connect(transport);
      this.client = client;
      this.retryDelay = INITIAL_RETRY_DELAY_MS;

      // Cache instructions from initialization handshake
      const initResult = (client as Record<string, unknown>)._instructions as string | undefined;
      if (initResult) {
        this.cachedInstructions = initResult;
      }

      // Re-register notification handler on the new client
      if (this.notificationHandler) {
        client.setNotificationHandler(
          { method: "notifications/resources/updated" } as Parameters<typeof client.setNotificationHandler>[0],
          this.notificationHandler
        );
      }

      this.log.info(`connected to ${this.url}`);

      // Re-subscribe to pending actions and catch up on missed results
      await this.resubscribePendingActions();
    } catch (err) {
      this.log.error(`failed to connect to approval gate: ${String(err)}`);
      this.scheduleReconnect();
    } finally {
      this.connecting = false;
    }
  }

  private scheduleReconnect(): void {
    const delay = this.retryDelay;
    this.retryDelay = Math.min(this.retryDelay * 2, MAX_RETRY_DELAY_MS);
    this.log.info(`reconnecting in ${delay}ms`);
    setTimeout(() => this.connect(), delay);
  }

  /**
   * Re-subscribe to all pending actions after reconnect.
   *
   * For each action: subscribe first (for future changes), then read current state
   * to catch results that arrived during the outage. If already terminal, deliver
   * the result immediately. The subscribe-then-read order avoids race conditions.
   */
  private async resubscribePendingActions(): Promise<void> {
    if (this.pendingActions.size === 0) return;
    this.log.info(`re-subscribing to ${this.pendingActions.size} pending action(s)`);
    const entries = [...this.pendingActions.entries()];
    for (const [actionId, { resourceUri }] of entries) {
      try {
        const client = this.requireClient();
        await client.subscribeResource({ uri: resourceUri });

        // Catch-up read: check if action resolved during outage
        const resource = await client.readResource({ uri: resourceUri });
        const content = resource.contents[0];
        if (content && "text" in content) {
          const action = JSON.parse((content as { text: string }).text) as Action;
          if (isTerminal(action.state.status)) {
            // Trigger the notification handler to deliver the result
            const notification = { method: "notifications/resources/updated", params: { uri: resourceUri } };
            this.notificationHandler?.(notification).catch((err) =>
              this.log.warn(`catch-up notification failed for ${actionId}: ${String(err)}`)
            );
          }
        }
      } catch (err) {
        this.log.warn(`failed to re-subscribe to action ${actionId}: ${String(err)}`);
      }
    }
  }

  private requireClient(): Client {
    if (!this.client) {
      throw new Error("approval gate server is currently unavailable");
    }
    return this.client;
  }

  get connected(): boolean {
    return this.client !== null;
  }

  get instructions(): string | undefined {
    return this.cachedInstructions;
  }

  setNotificationHandler(handler: NotificationHandler): void {
    this.notificationHandler = handler;
    if (this.client) {
      this.client.setNotificationHandler(
        { method: "notifications/resources/updated" } as Parameters<typeof this.client.setNotificationHandler>[0],
        handler
      );
    }
  }

  trackAction(actionId: string, resourceUri: string, sessionKey: string | null): void {
    this.pendingActions.set(actionId, { resourceUri, sessionKey });
  }

  resolveAction(actionId: string): void {
    this.pendingActions.delete(actionId);
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<ReturnType<Client["callTool"]>> {
    return this.requireClient().callTool({ name, arguments: args });
  }

  async readResource(uri: string): Promise<Action> {
    const client = this.requireClient();
    const resource = await client.readResource({ uri });
    const content = resource.contents[0];
    if (!content || !("text" in content)) {
      throw new Error(`resource ${uri} returned non-text content`);
    }
    return JSON.parse((content as { text: string }).text) as Action;
  }

  async subscribeResource(uri: string): Promise<void> {
    this.requireClient().subscribeResource({ uri });
  }

  async unsubscribeResource(uri: string): Promise<void> {
    try {
      this.requireClient().unsubscribeResource({ uri });
    } catch {
      // Swallow — unsubscribe is best-effort (connection may already be gone)
    }
  }

  async listTools(): Promise<ReturnType<Client["listTools"]>> {
    return this.requireClient().listTools();
  }
}

type GatewayReqFrame = { type: "req"; id: string; method: string; params?: unknown };
type GatewayResFrame = { type: "res"; id: string; ok: boolean; payload?: unknown; error?: unknown };
type GatewayFrame = GatewayReqFrame | GatewayResFrame | { type: string; [key: string]: unknown };

type PendingCall = { resolve: () => void; reject: (err: Error) => void; timeout: NodeJS.Timeout };
type ScopedLogger = ReturnType<typeof scopedLogger>;

/**
 * Persistent WebSocket connection to the OpenClaw gateway.
 *
 * Uses the gateway wire protocol: frames are `{type:"req"|"res"|"event", id, method, params}`.
 * Authenticates via the `connect` request (token auth, operator.admin scope) then reuses
 * the socket for `chat.inject` calls. Automatically reconnects on close; queues calls
 * that arrive before authentication completes.
 */
class GatewayConnection {
  private ws: WebSocket | null = null;
  private authenticated = false;
  private readonly pending = new Map<string, PendingCall>();
  private readonly preAuthQueue: Array<() => void> = [];
  private reqCounter = 0;

  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly logger: ScopedLogger
  ) {
    this.connect();
  }

  private send(method: string, params?: unknown): string {
    const id = `req-${(this.reqCounter += 1)}`;
    const frame: GatewayReqFrame = { type: "req", id, method, params };
    this.ws!.send(JSON.stringify(frame));
    return id;
  }

  private connect(): void {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    this.authenticated = false;

    ws.on("open", () => {
      // Authenticate immediately via the gateway `connect` request (token auth).
      const id = this.send("connect", {
        minProtocol: 3,
        maxProtocol: 3,
        client: {
          id: "approval-gate-plugin",
          displayName: "approval-gate plugin",
          version: "0.1.0",
          platform: "plugin",
          mode: "ui",
        },
        role: "operator",
        scopes: ["operator.admin"],
        caps: [],
        auth: { token: this.token },
      });
      // Resolve the connect response via the pending map.
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        this.logger.error("gateway connect timed out");
        ws.close();
      }, 12_000);
      this.pending.set(id, {
        resolve: () => {
          this.authenticated = true;
          for (const send of this.preAuthQueue.splice(0)) send();
        },
        reject: (err) => {
          this.logger.error(`gateway connect failed: ${err.message}`);
          ws.close();
        },
        timeout,
      });
    });

    ws.on("message", (data: Buffer | string) => {
      let frame: GatewayFrame;
      try {
        frame = JSON.parse(data.toString()) as GatewayFrame;
      } catch {
        return;
      }
      if (!frame || frame.type !== "res") return;
      const res = frame as GatewayResFrame;
      const entry = this.pending.get(res.id);
      if (!entry) return;
      clearTimeout(entry.timeout);
      this.pending.delete(res.id);
      if (res.ok) {
        entry.resolve();
      } else {
        entry.reject(new Error(JSON.stringify(res.error ?? res)));
      }
    });

    ws.on("error", (err: Error) => {
      this.logger.warn(`gateway WebSocket error: ${err.message}`);
    });

    ws.on("close", () => {
      this.authenticated = false;
      this.ws = null;
      for (const [, entry] of this.pending) {
        clearTimeout(entry.timeout);
        entry.reject(new Error("gateway connection closed"));
      }
      this.pending.clear();
      setTimeout(() => this.connect(), 5_000);
    });
  }

  inject(sessionKey: string, message: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const send = () => {
        const id = this.send("chat.inject", { sessionKey, message });
        const timeout = setTimeout(() => {
          this.pending.delete(id);
          reject(new Error("chat.inject timeout"));
        }, 10_000);
        this.pending.set(id, { resolve, reject, timeout });
      };
      if (this.authenticated) {
        send();
      } else {
        this.preAuthQueue.push(send);
      }
    });
  }
}

// ── Scoped logger ─────────────────────────────────────────────────────────────

function scopedLogger(api: OpenClawPluginApi) {
  const { logger } = api;
  return {
    info: (msg: string) => logger.info(`approval-gate: ${msg}`),
    warn: (msg: string) => logger.warn(`approval-gate: ${msg}`),
    error: (msg: string) => logger.error(`approval-gate: ${msg}`),
  };
}

// ── Plugin entry point ────────────────────────────────────────────────────────

export default async function register(api: OpenClawPluginApi): Promise<void> {
  const log = scopedLogger(api);
  const cfg = api.pluginConfig as { approvalGateUrl?: string; agentApiKey?: string } | undefined;

  const approvalGateUrl = cfg?.approvalGateUrl?.trim();
  const agentApiKey = cfg?.agentApiKey?.trim();

  if (!approvalGateUrl || !agentApiKey) {
    log.warn("approvalGateUrl and agentApiKey are required in plugin config; plugin disabled");
    return;
  }

  // ── Gateway WebSocket connection (long-lived, authenticates once) ─────────
  const gatewayToken = process.env.OPENCLAW_GATEWAY_TOKEN?.trim() ?? process.env.CLAWDBOT_GATEWAY_TOKEN?.trim();
  const gateway = gatewayToken
    ? new GatewayConnection(process.env.OPENCLAW_GATEWAY_WS_URL?.trim() ?? DEFAULT_GATEWAY_WS_URL, gatewayToken, log)
    : null;
  if (!gateway) {
    log.warn("OPENCLAW_GATEWAY_TOKEN not set; action results will not be injected into sessions");
  }

  // ── Resilient MCP connection to approval gate server ──────────────────────
  const connection = new ApprovalGateConnection(approvalGateUrl, agentApiKey, log);

  // Set up ResourceUpdated notification handler before connecting, so it's
  // registered on every (re)connect. The handler reads the action resource,
  // delivers terminal results via gateway.inject, and cleans up tracking.
  connection.setNotificationHandler(async (notification) => {
    const uri = (notification.params as { uri?: string }).uri;
    if (!uri?.startsWith(ACTION_RESOURCE_PREFIX)) return;

    const actionId = uri.slice(ACTION_RESOURCE_PREFIX.length);

    let action: Action;
    try {
      action = await connection.readResource(uri);
    } catch (err) {
      log.warn(`failed to read resource ${uri}: ${String(err)}`);
      return;
    }

    const { status } = action.state;
    if (!isTerminal(status)) return;

    // Terminal — clean up subscription and tracking
    connection.resolveAction(actionId);
    await connection.unsubscribeResource(uri);

    const sessionKey = action.session_key;
    if (!sessionKey) {
      log.info(`action ${actionId} has no session_key; skipping chat.inject`);
      return;
    }

    if (!gateway) return;

    const message = formatOutcomeMessage(action);
    try {
      await gateway.inject(sessionKey, message);
      log.info(`injected result for action ${actionId} into session ${sessionKey}`);
    } catch (err) {
      log.error(`failed to inject result for action ${actionId}: ${String(err)}`);
    }
  });

  try {
    await connection.connect();
  } catch (err) {
    log.error(`initial connection failed: ${String(err)} — will retry in background`);
  }

  // ── Discover and re-register approval gate tools ──────────────────────────
  // Tools are registered once at startup. If the initial connection fails,
  // tools won't be available until a manual restart. Once registered, tools
  // survive reconnections — execute() checks connection state and returns
  // a user-friendly error if the server is temporarily unavailable.
  if (!connection.connected) {
    log.warn("not connected on startup; no tools registered — plugin will reconnect in background");
    return;
  }

  let toolList: Awaited<ReturnType<Client["listTools"]>>;
  try {
    toolList = await connection.listTools();
  } catch (err) {
    log.error(`failed to list tools: ${String(err)}`);
    return;
  }

  for (const tool of toolList.tools) {
    const toolName = tool.name;
    const toolDescription = tool.description ?? "";
    // session_key is injected by us; agents should not see or set it
    const schema = stripSessionKey((tool.inputSchema ?? {}) as Record<string, unknown>);

    api.registerTool((ctx: OpenClawPluginToolContext) => ({
      name: toolName,
      label: toolName,
      description: toolDescription,
      parameters: schema,
      async execute(_id: string, params: Record<string, unknown>) {
        const callArgs = { ...params, session_key: ctx.sessionKey ?? null };

        let result: Awaited<ReturnType<Client["callTool"]>>;
        try {
          result = await connection.callTool(toolName, callArgs);
        } catch (err) {
          return {
            content: [
              {
                type: "text" as const,
                text: `Approval gate server is currently unavailable. Please retry shortly. (${String(err)})`,
              },
            ],
          };
        }

        const firstContent = result.content?.[0] as { text: string };
        const actionId = JSON.parse(firstContent.text) as string;
        const resourceUri = `${ACTION_RESOURCE_PREFIX}${actionId}`;

        // Track this action for re-subscription on reconnect
        connection.trackAction(actionId, resourceUri, ctx.sessionKey ?? null);

        // Subscribe so ResourceUpdated notifications reach our handler above.
        try {
          await connection.subscribeResource(resourceUri);
        } catch (err) {
          log.warn(`failed to subscribe to ${resourceUri}: ${String(err)}`);
        }

        // Read current state immediately — action may already be resolved
        // (auto-approved by predicate or instantly denied).
        let action: Action;
        try {
          action = await connection.readResource(resourceUri);
        } catch (err) {
          log.warn(`could not read initial state for ${resourceUri}: ${String(err)}`);
          return {
            content: [
              {
                type: "text" as const,
                text: `Action ${actionId} queued for operator approval`,
              },
            ],
          };
        }

        const { status } = action.state;
        if (isTerminal(status)) {
          // Already terminal — clean up and return outcome.
          connection.resolveAction(actionId);
          await connection.unsubscribeResource(resourceUri);
          return { content: [{ type: "text" as const, text: formatOutcomeMessage(action) }] };
        }

        // Still pending — tell the agent its action is queued.
        return {
          content: [
            {
              type: "text" as const,
              text: `Action ${actionId} queued for operator approval`,
            },
          ],
        };
      },
    }));
  }

  log.info(`registered ${toolList.tools.length} tool(s): ${toolList.tools.map((t) => t.name).join(", ")}`);

  // Inject MCP server instructions into the agent context on the first turn of each
  // fresh session (no user messages yet). prependContext ends up in the first user
  // message, so it gets included in the compaction summary when the history is
  // eventually compacted, giving the model a lasting understanding of how the gate works.
  //
  // The pseudo-XML envelope signals to the model that this is injected infrastructure
  // context, not a user message.
  const instructions = connection.instructions;
  if (instructions) {
    api.on("before_prompt_build", (event) => {
      const hasUserMessages = (event.messages as Array<{ role?: string }>).some((m) => m.role === "user");
      if (hasUserMessages) return;
      return {
        prependContext: `<approval-gate-instructions>\n${instructions}\n</approval-gate-instructions>`,
      };
    });
    log.info("registered before_prompt_build hook for instruction injection");
  }
}
