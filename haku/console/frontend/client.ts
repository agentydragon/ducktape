import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";

// Same-origin typed client (nginx serves this bundle and proxies /api). Types are
// generated from the backend's OpenAPI schema: //haku/console/frontend:schema.
const api = createClient<paths>({ baseUrl: "" });

export type ConfigResponse = components["schemas"]["ConfigResponse"];
export type LaunchRoutineResult = components["schemas"]["LaunchRoutineResult"];
export type ApprovalDecisionResponse = components["schemas"]["ApprovalDecisionResponse"];
type ApprovalDecisionRequest = components["schemas"]["ApprovalDecisionRequest"];
export type PendingApproval = components["schemas"]["PendingApproval"];
export type ToolCallRecord = components["schemas"]["ToolCallRecord"];
export type McpOperatorAuthStatus = components["schemas"]["McpOperatorAuthStatus"];
export type McpOperatorAuthStartResponse = components["schemas"]["McpOperatorAuthStartResponse"];

// FastAPI error responses are `{detail: string}`; surface that real reason rather
// than a generic message, falling back when the body isn't shaped that way.
function errorDetail(error: unknown, fallback: string): string {
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

async function fetchCsrfToken(): Promise<string> {
  const { data: csrf, error: csrfError } = await api.GET("/api/capabilities/csrf");
  if (csrfError || !csrf) throw new Error(errorDetail(csrfError, "Failed to get CSRF token"));
  return csrf.csrf_token;
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

export async function fetchPendingApprovals(): Promise<PendingApproval[]> {
  const { data, error } = await api.GET("/api/approvals/pending");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load pending approvals"));
  return data.approvals ?? [];
}

export async function fetchMcpOperatorAuthStatuses(): Promise<McpOperatorAuthStatus[]> {
  const { data, error } = await api.GET("/api/mcp/operator-auth");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load MCP account links"));
  return data.associations ?? [];
}

export async function startMcpOperatorAuth(serverId: string): Promise<McpOperatorAuthStartResponse> {
  const csrfToken = await fetchCsrfToken();
  const { data, error } = await api.POST("/api/mcp/operator-auth/{server_id}/start", {
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

export type GmailThreadPreview = components["schemas"]["GmailThreadPreview"];

// The google tool argument types (EventDateTime, CreateCalendarEventArgs, ...) aren't
// re-exported here: google_tool_previews.tsx gets both the runtime validator and the
// inferred TS type from :schema_zod (api/schema.zod.ts), generated from the same
// OpenAPI schema this file's `components["schemas"]` draws from — see
// `GoogleToolArgumentExamples` in haku/console/tools/google.py for why these models
// reach that schema even though nothing calls that endpoint for data.

// Live subject/snippet/current-labels lookup for rendering a batch_modify_gmail_thread_labels
// approval — the tool call's own arguments only carry thread IDs. Threads the operator's
// account can't resolve (deleted, wrong account, …) are simply absent from the map.
export async function fetchGmailThreadPreviews(threadIds: string[]): Promise<Record<string, GmailThreadPreview>> {
  if (threadIds.length === 0) return {};
  const { data, error } = await api.GET("/api/google/gmail/thread-previews", {
    params: { query: { thread_id: threadIds } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load Gmail thread previews"));
  return data.threads;
}
