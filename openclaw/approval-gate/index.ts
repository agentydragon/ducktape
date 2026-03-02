/**
 * Approval Gate OpenClaw plugin.
 *
 * Two responsibilities:
 *
 *   1. **Exec tool**: Registers an `exec` tool that calls the DirectExecServer
 *      sidecar (pod-local, unauthenticated). Injects `OPENCLAW_SESSION_ID` as
 *      an env var so the agent knows its session identity when calling external
 *      services (e.g. the approval gate via mcporter).
 *
 *   2. **Approval gate notifications**: Connects to the approval gate MCP server,
 *      subscribes to session log HWM resources, and delivers terminal action
 *      results (approved/denied/withdrawn) to the agent via OpenClaw's system
 *      notification queue (`enqueueSystemEvent`).
 *
 * Auth:
 *   - Exec sidecar: unauthenticated (pod-local, 127.0.0.1)
 *   - Approval gate MCP endpoint: Bearer token (approvalGateToken from plugin config)
 *
 * This plugin MUST run inside the gateway process (not a node). The
 * `enqueueSystemEvent` API writes to the gateway's in-memory per-session
 * event queue and would not work from a node process.
 *
 * Notification delivery uses `enqueueSystemEvent` (OpenClaw's in-memory
 * per-session event queue). Events are drained and prepended to the agent's
 * next prompt automatically by the heartbeat mechanism.
 */

import type { OpenClawPluginApi, OpenClawPluginToolContext } from "openclaw/plugin-sdk";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { ApprovalGateConnection, parseSessionKeyFromHwmUri } from "./approval-gate-connection.js";
import { ReconnectingMcpClient } from "./reconnecting-mcp-client.js";
import { scopedLogger } from "./util.js";
import type { Action, ActionKey, Detail as LogEventDetail, DoneState, RejectedState } from "./types.js";

const DEFAULT_EXEC_SERVER_URL = "http://127.0.0.1:8766/mcp";

// ── Helpers ───────────────────────────────────────────────────────────────────

const TERMINAL_LOG_KINDS = new Set(["execution_finished", "denied", "withdrawn"]);

function isTerminalLogKind(kind: string): boolean {
  return TERMINAL_LOG_KINDS.has(kind);
}

/** Format a human-readable message from a terminal log entry detail. */
function formatTerminalMessage(keyStr: string, detail: LogEventDetail): string {
  if (detail.kind === "execution_finished") {
    const parts = detail.outcome.content.filter((c) => c.type === "text" && c.text).map((c) => c.text as string);
    const body = parts.join("\n") || JSON.stringify(detail.outcome.content, null, 2);
    if (!detail.outcome.isError) {
      return `Action ${keyStr} approved and executed:\n\n${body}`;
    } else {
      return `Action ${keyStr} was approved but execution returned an error:\n\n${body}`;
    }
  }
  if (detail.kind === "denied") {
    return `Action ${keyStr} was rejected by the user. Reason: ${detail.reason ?? "none given"}`;
  }
  if (detail.kind === "withdrawn") {
    return `Action ${keyStr} was withdrawn.`;
  }
  return `Action ${keyStr} log event: ${detail.kind}`;
}

/** Strip named keys from a JSON schema's properties and required arrays. */
function stripSchemaKeys(schema: Record<string, unknown>, keys: string[]): Record<string, unknown> {
  const result = structuredClone(schema) as Record<string, unknown>;
  const props = result.properties as Record<string, unknown> | undefined;
  if (props) {
    for (const key of keys) delete props[key];
  }
  const required = result.required as string[] | undefined;
  if (required) {
    const keySet = new Set(keys);
    result.required = required.filter((k) => !keySet.has(k));
  }
  return result;
}

// ── Plugin entry point ────────────────────────────────────────────────────────

export default async function register(api: OpenClawPluginApi): Promise<void> {
  const log = scopedLogger(api, "approval-gate");
  const execLog = scopedLogger(api, "exec");
  const cfg = api.pluginConfig as
    | {
        approvalGate?: { url?: string; token?: string };
        execServer?: { url?: string };
        registerTools?: boolean;
      }
    | undefined;

  const approvalGateUrl = cfg?.approvalGate?.url?.trim();
  const approvalGateToken = cfg?.approvalGate?.token?.trim();
  const execServerUrl = cfg?.execServer?.url?.trim() ?? DEFAULT_EXEC_SERVER_URL;

  if (!approvalGateUrl || !approvalGateToken) {
    log.warn("approvalGate.url and approvalGate.token are required in plugin config; plugin disabled");
    return;
  }

  const { enqueueSystemEvent } = api.runtime.system;

  // ── Exec sidecar MCP connection ───────────────────────────────────────────
  const execConnection = new ReconnectingMcpClient(execServerUrl, "openclaw-exec", execLog);
  try {
    await execConnection.connect();
  } catch (err) {
    log.error(`exec server initial connection failed: ${String(err)} — will retry in background`);
  }

  // ── Register exec tool ────────────────────────────────────────────────────
  // Discover the exec tool schema from the sidecar MCP server, strip env/inherit_env
  // (we inject OPENCLAW_SESSION_ID ourselves), and re-register with OpenClaw.
  // TODO: Consider allowing env merging with session ID later.
  if (!execConnection.connected) {
    log.error("exec server not connected on startup; exec tool not registered");
  } else {
    let execToolList: Awaited<ReturnType<Client["listTools"]>>;
    try {
      execToolList = await execConnection.listTools();
    } catch (err) {
      log.error(`failed to list exec server tools: ${String(err)}`);
      execToolList = { tools: [] };
    }

    const execTools = execToolList.tools.filter((t) => t.name === "exec");
    if (execTools.length !== 1) {
      log.error(`expected exactly 1 exec tool from sidecar, got ${execTools.length}`);
    } else {
      const execTool = execTools[0];
      const execSchema = stripSchemaKeys((execTool.inputSchema ?? {}) as Record<string, unknown>, [
        "env",
        "inherit_env",
      ]);

      api.registerTool((ctx: OpenClawPluginToolContext) => ({
        name: "exec",
        label: "exec",
        description:
          "Execute a command in the exec container. " +
          "The workspace directory is shared with the gateway (same path). " +
          "OPENCLAW_SESSION_ID is automatically set in the environment.",
        parameters: execSchema,
        async execute(_id: string, params: Record<string, unknown>) {
          const env = [`OPENCLAW_SESSION_ID=${ctx.sessionKey ?? ""}`, `APPROVAL_GATE_URL=${approvalGateUrl}`];
          const cwd = (params.cwd as string | undefined) ?? ctx.workspaceDir;
          const callArgs = { ...params, env, ...(cwd ? { cwd } : {}) };

          try {
            return await execConnection.callTool("exec", callArgs);
          } catch (err) {
            return {
              content: [
                {
                  type: "text" as const,
                  text: `Exec server is currently unavailable. Please retry shortly. (${String(err)})`,
                },
              ],
            };
          }
        },
      }));
      log.info("registered exec tool (schema discovered from sidecar)");
    }
  }

  // ── Resilient MCP connection to approval gate server ──────────────────────
  const connection = new ApprovalGateConnection(approvalGateUrl, approvalGateToken, log);

  async function catchUpSession(sessionKey: string): Promise<void> {
    let newHwm: number;
    try {
      newHwm = await connection.readHwm(sessionKey);
    } catch (err) {
      log.warn(`failed to read HWM for session ${sessionKey}: ${String(err)}`);
      return;
    }

    const lastHwm = connection.getSessionHwm(sessionKey);
    if (newHwm <= lastHwm) return;

    const entryIds = Array.from({ length: newHwm - lastHwm }, (_, i) => lastHwm + 1 + i);
    const entries = await Promise.all(
      entryIds.map((id) =>
        connection.readLogEntry(sessionKey, id).catch((err) => {
          log.warn(`failed to read log entry ${sessionKey}/${id}: ${String(err)}`);
          return null;
        })
      )
    );

    for (const entry of entries) {
      if (!entry || !isTerminalLogKind(entry.detail.kind)) continue;

      const keyStr = `${sessionKey}/${entry.action_seq}`;
      const message = formatTerminalMessage(keyStr, entry.detail);
      enqueueSystemEvent(message, { sessionKey });
      log.info(`enqueued system event for action ${keyStr}`);
    }

    connection.updateSessionHwm(sessionKey, newHwm);
  }

  connection.setCatchUpHandler(catchUpSession);

  connection.setNotificationHandler(async (notification) => {
    const uri = (notification.params as { uri?: string }).uri;
    if (!uri) return;

    const sessionKey = parseSessionKeyFromHwmUri(uri);
    if (!sessionKey) return;

    await catchUpSession(sessionKey);
  });

  try {
    await connection.connect();
  } catch (err) {
    log.error(`initial connection failed: ${String(err)} — will retry in background`);
  }

  // ── Discover and re-register approval gate tools ──────────────────────────
  // Gated behind registerTools config (default off). When disabled, the plugin
  // still listens for action notifications and delivers results via system
  // events, but does not expose approval-gate-wrapped tools to agents.
  const registerTools = cfg?.registerTools ?? false;

  if (registerTools) {
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
      const schema = stripSchemaKeys((tool.inputSchema ?? {}) as Record<string, unknown>, ["session_key"]);

      api.registerTool((ctx: OpenClawPluginToolContext) => ({
        name: toolName,
        label: toolName,
        description: toolDescription,
        parameters: schema,
        async execute(_id: string, params: Record<string, unknown>) {
          const callArgs = { ...params, session_key: ctx.sessionKey };

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

          await connection.trackSession(actionKey.session_key);

          const actionUri = `resource://sessions/${actionKey.session_key}/actions/${actionKey.action_seq}`;
          try {
            const text = await connection.readResourceText(actionUri);
            const action = JSON.parse(text) as Action;
            const { status } = action.state;
            if (status === "done" || status === "rejected" || status === "withdrawn") {
              // TODO: The mapping from action state to log detail kind is awkward because
              // Action.state and LogEntry.detail have overlapping but different shapes.
              // Consider unifying the terminal state representation upstream.
              const outcome = formatTerminalMessage(keyStr, {
                kind: status === "done" ? "execution_finished" : status === "rejected" ? "denied" : "withdrawn",
                ...(status === "done" ? { outcome: (action.state as DoneState).outcome } : {}),
                ...(status === "rejected" ? { reason: (action.state as RejectedState).reason } : {}),
              } as LogEventDetail);
              return { content: [{ type: "text" as const, text: outcome }] };
            }
          } catch (err) {
            log.warn(`could not read initial state for ${actionUri}: ${String(err)}`);
          }

          return {
            content: [
              {
                type: "text" as const,
                text: `Action ${keyStr} queued for user approval`,
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

  // Agent instructions for approval gate usage are provided via an OpenClaw
  // skill (SKILL.md) in the repo, not injected here. The skill tells the agent
  // how to use mcporter for approval-gated actions and how to read results.
}
