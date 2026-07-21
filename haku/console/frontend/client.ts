import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";
import { redirectToOperatorLogin } from "./operator_login.ts";

// Same-origin typed client (nginx serves this bundle and proxies /api). Types are
// generated from the backend's OpenAPI schema: //haku/console/frontend:schema.
// Exported (not module-private) so per-integration client files (gmail_client.ts,
// grocy_client.ts) share this one instance instead of each creating their own.
export const api = createClient<paths>({ baseUrl: "" });

// App-owned operator auth: the /api/* router guards answer with 401 when there's no operator
// session (the console replaced the Authentik forward-auth outpost with its own OIDC login). The
// SPA itself is served publicly, so on that 401 bounce the browser to /auth/login to (re)establish
// the session; Authentik's application access policy decides who may complete it. In the
// operator_oidc-unset dev/test mode the guards no-op and /api never 401s, so this never fires there.
api.use({
  onResponse({ response }) {
    if (response.status === 401 && typeof window !== "undefined") redirectToOperatorLogin();
    return response;
  },
});

export type ConfigResponse = components["schemas"]["ConfigResponse"];
export type DeploymentInfo = components["schemas"]["DeploymentInfo"];
export type LaunchRoutineResult = components["schemas"]["LaunchRoutineResult"];
export type ApprovalDecisionResponse = components["schemas"]["ApprovalDecisionResponse"];
type ApprovalDecisionRequest = components["schemas"]["ApprovalDecisionRequest"];
export type ToolCallRecord = components["schemas"]["ToolCallRecord"];
export type McpOperatorAuthConnectResponse = components["schemas"]["McpOperatorAuthConnectResponse"];
export type McpOperatorAuthStatus = components["schemas"]["McpOperatorAuthStatus"];
export type ProviderConnectionConnectResponse = components["schemas"]["ProviderConnectionConnectResponse"];
export type OperatorConnectionName = ProviderConnectionConnectResponse["connection"];

// FastAPI error responses are `{detail: string}`; surface that real reason rather
// than a generic message, falling back when the body isn't shaped that way. Exported
// for per-integration client files to reuse the same error-unwrapping convention.
export function errorDetail(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "detail" in error) {
    const { detail } = error as { detail: unknown };
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

export async function fetchConfig(): Promise<ConfigResponse> {
  const { data, error } = await api.GET("/api/config");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load config"));
  return data;
}

export async function fetchDeploymentInfo(): Promise<DeploymentInfo> {
  const { data, error } = await api.GET("/api/deployment");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load deployment information"));
  return data;
}

// fastapi-csrf-protect signs these for one hour. Reuse one token/cookie pair across concurrent
// console and MCP requests, but refresh with margin so a long-open tab never relies on an expired
// token. mcp_client reads this cache for every transport request, so a refresh cannot strand it
// with an older header than the browser's newly rotated HttpOnly cookie.
const CSRF_TOKEN_CACHE_MS = 50 * 60 * 1000;
let csrfToken: Promise<string> | null = null;
let csrfTokenExpiresAt = 0;

async function requestCsrfToken(): Promise<string> {
  const { data: csrf, error: csrfError } = await api.GET("/api/capabilities/csrf");
  if (csrfError || !csrf) throw new Error(errorDetail(csrfError, "Failed to get CSRF token"));
  return csrf.csrf_token;
}

export async function fetchCsrfToken(): Promise<string> {
  if (csrfToken === null || Date.now() >= csrfTokenExpiresAt) {
    csrfTokenExpiresAt = Date.now() + CSRF_TOKEN_CACHE_MS;
    csrfToken = requestCsrfToken().catch((error: unknown) => {
      csrfToken = null;
      csrfTokenExpiresAt = 0;
      throw error;
    });
  }
  return csrfToken;
}

export async function refreshCsrfToken(): Promise<string> {
  csrfToken = null;
  csrfTokenExpiresAt = 0;
  return fetchCsrfToken();
}

// Capability tier. Fetch a CSRF token (which also sets the signed double-submit
// cookie), then fire the routine echoing the token in X-CSRF-Token. The bearer
// stays server-side; this only triggers the action and returns the new session URL.
export async function launchRoutine(text?: string): Promise<LaunchRoutineResult> {
  const csrfToken = await fetchCsrfToken();
  const { data, error } = await api.POST("/api/capabilities/launch-routine", {
    headers: { "X-CSRF-Token": csrfToken },
    body: text ? { text } : {},
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to launch routine"));
  return data;
}

export async function fetchPendingApprovals(): Promise<ToolCallRecord[]> {
  const { data, error } = await api.GET("/api/approvals/pending");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load pending approvals"));
  return data.approvals ?? [];
}

// The full tool-call audit ledger for the history view: newest first, so `limit` keeps
// the most recent calls when the ledger has grown past it.
export async function fetchToolCalls(limit: number): Promise<ToolCallRecord[]> {
  const { data, error } = await api.GET("/api/tool-calls", {
    params: { query: { newest_first: true, limit } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load tool calls"));
  return data.tool_calls ?? [];
}

export async function connectMcpOperatorAuth(serverId: string): Promise<McpOperatorAuthConnectResponse> {
  const csrfToken = await fetchCsrfToken();
  const { data, error } = await api.POST("/api/mcp/operator-auth/{server_id}/connect", {
    params: { path: { server_id: serverId } },
    headers: { "X-CSRF-Token": csrfToken },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to start MCP account link"));
  return data;
}

export async function disconnectMcpOperatorAuth(serverId: string): Promise<McpOperatorAuthStatus> {
  const csrfToken = await fetchCsrfToken();
  const { data, error } = await api.DELETE("/api/mcp/operator-auth/{server_id}", {
    params: { path: { server_id: serverId } },
    headers: { "X-CSRF-Token": csrfToken },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to disconnect MCP account"));
  return data;
}

// Per-Operator external account connections (Google today), the console's own replacement for
// Airlock's brokered token. Connect opens the provider's consent in a new tab; the backend
// callback stores the refresh token and broadcasts an `operator_connection_changed` event.
export async function connectOperatorConnection(
  connection: OperatorConnectionName
): Promise<ProviderConnectionConnectResponse> {
  const csrfToken = await fetchCsrfToken();
  const { data, error } = await api.POST("/api/operator-connections/{connection}/connect", {
    params: { path: { connection } },
    headers: { "X-CSRF-Token": csrfToken },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to start account connection"));
  return data;
}

export async function disconnectOperatorConnection(
  connection: OperatorConnectionName
): Promise<components["schemas"]["ProviderUnconnected"]> {
  const csrfToken = await fetchCsrfToken();
  const { data, error } = await api.DELETE("/api/operator-connections/{connection}", {
    params: { path: { connection } },
    headers: { "X-CSRF-Token": csrfToken },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to disconnect account"));
  return data;
}

async function decideToolCall(
  toolCallId: string,
  body: ApprovalDecisionRequest,
  fallback: string
): Promise<ToolCallRecord> {
  const csrfToken = await fetchCsrfToken();
  const { data, error } = await api.POST("/api/tool-calls/{tool_call_id}/decision", {
    params: { path: { tool_call_id: toolCallId } },
    headers: { "X-CSRF-Token": csrfToken },
    body,
  });
  if (error || !data) throw new Error(errorDetail(error, fallback));
  return data.tool_call;
}

export async function approveToolCall(toolCallId: string): Promise<ToolCallRecord> {
  return decideToolCall(toolCallId, { decision: "approve" }, "Failed to approve tool call");
}

export async function denyToolCall(toolCallId: string, reason?: string): Promise<ToolCallRecord> {
  return decideToolCall(toolCallId, { decision: "deny", reason: reason ?? null }, "Failed to deny tool call");
}
