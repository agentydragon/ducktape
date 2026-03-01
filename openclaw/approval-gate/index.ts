/**
 * Approval Gate OpenClaw plugin.
 *
 * Connects to the approval gate MCP server as a persistent client and:
 *   1. Discovers approval-gate tools via MCP list_tools.
 *   2. Re-registers each tool with OpenClaw, stripping `session_key` from the
 *      schema (injected automatically from ctx.sessionKey).
 *   3. Subscribes to `resource://sessions/{session_key}/log_hwm` MCP resource
 *      notifications for each session.
 *   4. On HWM change: catches up by reading missed log entries. For terminal
 *      events (done/rejected/withdrawn), reads the full action state and
 *      injects the result into the agent session via chat.inject (local gateway
 *      WebSocket).
 *
 * Resilience: if the approval gate MCP server goes down, the plugin automatically
 * reconnects with exponential backoff. On reconnect, it re-subscribes to all
 * tracked sessions' log HWMs and catches up from the last known HWM.
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
const LOG_HWM_PREFIX = "resource://sessions/";
const LOG_HWM_SUFFIX = "/log_hwm";
const INITIAL_RETRY_DELAY_MS = 5_000;
const MAX_RETRY_DELAY_MS = 60_000;

// ── Action/log types (mirrors approval_gate/models.py) ──────────────────────
// TODO: These types overlap with approval_gate/frontend/types.ts. The two packages live in
// different environments (Node.js plugin vs browser SPA) and carry different field sets, so
// a shared package would add more complexity than it removes for now. If a third consumer
// appears, consider extracting a shared @ducktape/approval-gate-types workspace package.

interface ActionKey {
  session_key: string;
  action_seq: number;
}

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
  key: ActionKey;
  call: { tool_name: string };
  state: ActionState;
}

interface LogEntry {
  entry_id: number;
  session_key: string;
  action_seq: number;
  kind: string;
  timestamp: string;
  detail_json: string | null;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

type TerminalStatus = "done" | "rejected" | "withdrawn";
const TERMINAL_STATUSES = new Set<TerminalStatus>(["done", "rejected", "withdrawn"]);
const TERMINAL_LOG_KINDS = new Set(["execution_finished", "denied", "withdrawn"]);

function isTerminal(status: string): status is TerminalStatus {
  return TERMINAL_STATUSES.has(status as TerminalStatus);
}

function isTerminalLogKind(kind: string): boolean {
  return TERMINAL_LOG_KINDS.has(kind);
}

function formatOutcomeMessage(action: Action): string {
  const state = action.state;
  const keyStr = `${action.key.session_key}/${action.key.action_seq}`;

  if (state.status === "done") {
    const done = state as DoneState;
    const parts = done.outcome.content.filter((c) => c.type === "text" && c.text).map((c) => c.text as string);
    const body = parts.join("\n") || JSON.stringify(done.outcome.content, null, 2);
    if (!done.outcome.isError) {
      return `Action ${keyStr} approved and executed:\n\n${body}`;
    } else {
      return `Action ${keyStr} was approved but execution returned an error:\n\n${body}`;
    }
  }
  if (state.status === "rejected") {
    const rej = state as RejectedState;
    return `Action ${keyStr} was rejected by the operator. Reason: ${rej.reason ?? "none given"}`;
  }
  if (state.status === "withdrawn") {
    return `Action ${keyStr} was withdrawn.`;
  }
  return `Action ${keyStr} state changed to: ${state.status}`;
}

/** Parse session_key from a log HWM URI like resource://sessions/{key}/log_hwm */
function parseSessionKeyFromHwmUri(uri: string): string | null {
  if (!uri.startsWith(LOG_HWM_PREFIX) || !uri.endsWith(LOG_HWM_SUFFIX)) return null;
  return uri.slice(LOG_HWM_PREFIX.length, uri.length - LOG_HWM_SUFFIX.length);
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

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type NotificationHandler = (notification: any) => Promise<void>;

// ── ApprovalGateConnection ──────────────────────────────────────────────────

/**
 * Resilient MCP client connection to the approval gate server.
 *
 * Automatically reconnects on disconnect with exponential backoff. Tracks
 * session HWMs and re-subscribes on reconnect, performing catch-up reads
 * to deliver results that arrived during the outage.
 */
class ApprovalGateConnection {
  private client: Client | null = null;
  private connecting = false;
  private retryDelay = INITIAL_RETRY_DELAY_MS;
  private notificationHandler: NotificationHandler | null = null;
  private cachedInstructions: string | undefined;
  /** Last seen log entry_id per session — re-subscribed on reconnect. */
  private readonly sessionHwms = new Map<string, number>();

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

      // Re-subscribe to tracked sessions and catch up on missed log entries
      await this.resubscribeTrackedSessions();
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
   * Re-subscribe to all tracked sessions after reconnect.
   *
   * For each session: subscribe to its log_hwm resource, then read the current
   * HWM and catch up on any missed log entries.
   */
  private async resubscribeTrackedSessions(): Promise<void> {
    if (this.sessionHwms.size === 0) return;
    this.log.info(`re-subscribing to ${this.sessionHwms.size} tracked session(s)`);
    for (const [sessionKey, lastHwm] of this.sessionHwms.entries()) {
      try {
        const hwmUri = `${LOG_HWM_PREFIX}${sessionKey}${LOG_HWM_SUFFIX}`;
        const client = this.requireClient();
        await client.subscribeResource({ uri: hwmUri });

        // Read current HWM and catch up
        const currentHwm = await this.readHwm(sessionKey);
        if (currentHwm > lastHwm) {
          // Trigger catch-up via the notification handler
          const notification = { method: "notifications/resources/updated", params: { uri: hwmUri } };
          this.notificationHandler?.(notification).catch((err) =>
            this.log.warn(`catch-up failed for session ${sessionKey}: ${String(err)}`)
          );
        }
      } catch (err) {
        this.log.warn(`failed to re-subscribe to session ${sessionKey}: ${String(err)}`);
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

  /** Start tracking a session's log HWM. Subscribes if not already tracked. */
  async trackSession(sessionKey: string): Promise<void> {
    if (this.sessionHwms.has(sessionKey)) return;
    this.sessionHwms.set(sessionKey, 0);
    const hwmUri = `${LOG_HWM_PREFIX}${sessionKey}${LOG_HWM_SUFFIX}`;
    try {
      await this.requireClient().subscribeResource({ uri: hwmUri });
      this.log.info(`tracking session ${sessionKey}`);
    } catch (err) {
      this.log.warn(`failed to subscribe to HWM for session ${sessionKey}: ${String(err)}`);
    }
  }

  /** Update the last-seen HWM for a session. */
  updateSessionHwm(sessionKey: string, hwm: number): void {
    const current = this.sessionHwms.get(sessionKey) ?? 0;
    if (hwm > current) {
      this.sessionHwms.set(sessionKey, hwm);
    }
  }

  /** Get the last-seen HWM for a session. */
  getSessionHwm(sessionKey: string): number {
    return this.sessionHwms.get(sessionKey) ?? 0;
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

  async readResourceText(uri: string): Promise<string> {
    const client = this.requireClient();
    const resource = await client.readResource({ uri });
    const content = resource.contents[0];
    if (!content || !("text" in content)) {
      throw new Error(`resource ${uri} returned non-text content`);
    }
    return (content as { text: string }).text;
  }

  async readHwm(sessionKey: string): Promise<number> {
    const uri = `${LOG_HWM_PREFIX}${sessionKey}${LOG_HWM_SUFFIX}`;
    const text = await this.readResourceText(uri);
    return parseInt(text, 10);
  }

  async readLogEntry(sessionKey: string, entryId: number): Promise<LogEntry> {
    const uri = `resource://sessions/${sessionKey}/log/${entryId}`;
    const text = await this.readResourceText(uri);
    return JSON.parse(text) as LogEntry;
  }

  async subscribeResource(uri: string): Promise<void> {
    this.requireClient().subscribeResource({ uri });
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
  const cfg = api.pluginConfig as
    | { approvalGateUrl?: string; agentApiKey?: string; registerTools?: boolean }
    | undefined;

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
  // registered on every (re)connect. The handler watches for log HWM changes,
  // catches up on missed log entries, and delivers terminal results via gateway.inject.
  connection.setNotificationHandler(async (notification) => {
    const uri = (notification.params as { uri?: string }).uri;
    if (!uri) return;

    const sessionKey = parseSessionKeyFromHwmUri(uri);
    if (!sessionKey) return;

    // Read the new HWM value
    let newHwm: number;
    try {
      newHwm = await connection.readHwm(sessionKey);
    } catch (err) {
      log.warn(`failed to read HWM for session ${sessionKey}: ${String(err)}`);
      return;
    }

    const lastHwm = connection.getSessionHwm(sessionKey);
    if (newHwm <= lastHwm) return;

    // Catch up on log entries since our last seen HWM
    for (let entryId = lastHwm + 1; entryId <= newHwm; entryId++) {
      let entry: LogEntry;
      try {
        entry = await connection.readLogEntry(sessionKey, entryId);
      } catch (err) {
        log.warn(`failed to read log entry ${sessionKey}/${entryId}: ${String(err)}`);
        continue;
      }

      if (!isTerminalLogKind(entry.kind)) continue;

      // Read the full action state for terminal events
      const actionUri = `resource://sessions/${sessionKey}/actions/${entry.action_seq}`;
      let action: Action;
      try {
        action = await connection.readResource(actionUri);
      } catch (err) {
        log.warn(`failed to read action ${sessionKey}/${entry.action_seq}: ${String(err)}`);
        continue;
      }

      if (!isTerminal(action.state.status)) continue;
      if (!gateway) continue;

      const message = formatOutcomeMessage(action);
      try {
        await gateway.inject(sessionKey, message);
        log.info(`injected result for action ${sessionKey}/${entry.action_seq} into session ${sessionKey}`);
      } catch (err) {
        log.error(`failed to inject result for action ${sessionKey}/${entry.action_seq}: ${String(err)}`);
      }
    }

    connection.updateSessionHwm(sessionKey, newHwm);
  });

  try {
    await connection.connect();
  } catch (err) {
    log.error(`initial connection failed: ${String(err)} — will retry in background`);
  }

  // ── Discover and re-register approval gate tools ──────────────────────────
  // Gated behind registerTools config (default off). When disabled, the plugin
  // still listens for action notifications and delivers results via chat.inject,
  // but does not expose approval-gate-wrapped tools to agents.
  const registerTools = cfg?.registerTools ?? false;

  if (registerTools) {
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
          const actionKey = JSON.parse(firstContent.text) as ActionKey;
          const keyStr = `${actionKey.session_key}/${actionKey.action_seq}`;

          // Start tracking this session's log HWM for notifications
          await connection.trackSession(actionKey.session_key);

          // Read current state immediately — action may already be resolved
          // (auto-approved by predicate or instantly denied).
          const actionUri = `resource://sessions/${actionKey.session_key}/actions/${actionKey.action_seq}`;
          let action: Action;
          try {
            action = await connection.readResource(actionUri);
          } catch (err) {
            log.warn(`could not read initial state for ${actionUri}: ${String(err)}`);
            return {
              content: [
                {
                  type: "text" as const,
                  text: `Action ${keyStr} queued for operator approval`,
                },
              ],
            };
          }

          const { status } = action.state;
          if (isTerminal(status)) {
            // Already terminal — return outcome directly.
            return { content: [{ type: "text" as const, text: formatOutcomeMessage(action) }] };
          }

          // Still pending — tell the agent its action is queued.
          return {
            content: [
              {
                type: "text" as const,
                text: `Action ${keyStr} queued for operator approval`,
              },
            ],
          };
        },
      }));
    }

    log.info(`registered ${toolList.tools.length} tool(s): ${toolList.tools.map((t) => t.name).join(", ")}`);
  } else {
    log.info("registerTools is off; skipping tool registration (notification listener still active)");
  }

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
