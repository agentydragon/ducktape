import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";
import { operatorLoginRedirectStarted, redirectToOperatorLogin } from "./operator_login";

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
export type OperatorResponse = components["schemas"]["OperatorResponse"];
export type DeploymentInfo = components["schemas"]["DeploymentInfo"];
export type LaunchRoutineResult = components["schemas"]["LaunchRoutineResult"];
export type ApprovalDecisionResponse = components["schemas"]["ApprovalDecisionResponse"];
type ApprovalDecisionRequest = components["schemas"]["ApprovalDecisionRequest"];
export type ToolCallRecord = components["schemas"]["ToolCallRecord"];
export type McpOperatorAuthConnectResponse = components["schemas"]["McpOperatorAuthConnectResponse"];
export type McpOperatorAuthStatus = components["schemas"]["McpOperatorAuthStatus"];
export type ProviderConnectionConnectResponse = components["schemas"]["ProviderConnectionConnectResponse"];
export type OperatorConnectionName = ProviderConnectionConnectResponse["connection"];
export type OAuthConnectionResult =
  | components["schemas"]["OAuthConnectionSucceeded"]
  | components["schemas"]["OAuthConnectionFailed"];
export type AgentView = components["schemas"]["AgentView"];
export type ClaudeChatSession = components["schemas"]["SessionView"];
export type ClaudeChatMessage = components["schemas"]["SessionMessageView"];
export type ConversationSessionSummary = components["schemas"]["ConversationSessionSummary"];
export type ConversationSession = components["schemas"]["ConversationSessionView"];
export type AgentListResponse = components["schemas"]["AgentListResponse"];
export type EnrollmentView = components["schemas"]["EnrollmentView"];
export type EnrollmentDecisionRequest =
  | components["schemas"]["CreateEnrollmentRequest"]
  | components["schemas"]["ReconnectEnrollmentRequest"]
  | components["schemas"]["DenyEnrollmentRequest"];
export type EnrollmentDecisionResponse =
  | components["schemas"]["EnrollmentContinues"]
  | components["schemas"]["EnrollmentWasDenied"];

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

// Every fetch-error surface in the SPA holds `string | null`, so null means "nothing to show".
// That is the right answer once a 401 has started the login redirect (the middleware above): this
// document is about to be replaced, and reporting the failure of the request that triggered its own
// redirect just flashes the API's detail string at an operator being signed straight back in.
export function displayableError(e: unknown): string | null {
  if (operatorLoginRedirectStarted()) return null;
  return e instanceof Error ? e.message : String(e);
}

export async function fetchConfig(): Promise<ConfigResponse> {
  const { data, error } = await api.GET("/api/config");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load config"));
  return data;
}

/** The signed-in operator and the absolute deadline their session stops being accepted at. */
export async function fetchOperator(): Promise<OperatorResponse> {
  const { data, error } = await api.GET("/auth/me");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load the operator session"));
  return data;
}

export async function createClaudeChatSession(): Promise<ClaudeChatSession> {
  const { data, error } = await api.POST("/api/sessions");
  if (error || !data) throw new Error(errorDetail(error, "Failed to create Claude chat session"));
  return data;
}

export async function fetchClaudeChatSession(sessionId: string): Promise<ClaudeChatSession> {
  const { data, error } = await api.GET("/api/sessions/{session_id}", {
    params: { path: { session_id: sessionId } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load Claude chat session"));
  return data;
}

export async function fetchConversations(limit = 50): Promise<ConversationSessionSummary[]> {
  const { data, error } = await api.GET("/api/conversations", { params: { query: { limit } } });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load conversations"));
  return data;
}

export async function fetchConversation(sessionId: string): Promise<ConversationSession> {
  const { data, error } = await api.GET("/api/conversations/{session_id}", {
    params: { path: { session_id: sessionId } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load conversation"));
  return data;
}

export async function sendClaudeChatMessage(sessionId: string, text: string): Promise<ClaudeChatMessage> {
  const { data, error } = await api.POST("/api/sessions/{session_id}/messages", {
    params: { path: { session_id: sessionId } },
    body: { text },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to send Claude chat message"));
  return data;
}

export async function deleteClaudeChatSession(sessionId: string): Promise<void> {
  const { error } = await api.DELETE("/api/sessions/{session_id}", {
    params: { path: { session_id: sessionId } },
  });
  if (error) throw new Error(errorDetail(error, "Failed to close Claude chat session"));
}

export async function fetchDeploymentInfo(): Promise<DeploymentInfo> {
  const { data, error } = await api.GET("/api/deployment");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load deployment information"));
  return data;
}

export async function consumeOAuthConnectionResult(resultId: string): Promise<OAuthConnectionResult> {
  const { data, error } = await api.POST("/api/oauth-results/{result_id}", {
    params: { path: { result_id: resultId } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load the connection result"));
  return data;
}

export async function listAgents(): Promise<AgentListResponse> {
  const { data, error } = await api.GET("/api/agent-enrollment/agents");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load Agents"));
  return data;
}

export async function updateAgentAutoApprovalPolicy(agentId: string, autoApprovalPolicy: string): Promise<AgentView> {
  const { data, error } = await api.PUT("/api/agent-enrollment/agents/{agent_id}/auto-approval-policy", {
    params: { path: { agent_id: agentId } },
    body: { auto_approval_policy: autoApprovalPolicy },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to update Agent auto-approval policy"));
  return data;
}

export async function getAgentEnrollment(interactionId: string): Promise<EnrollmentView> {
  const { data, error } = await api.GET("/api/agent-enrollment/{interaction_id}", {
    params: { path: { interaction_id: interactionId } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load Agent enrollment"));
  return data;
}

export async function decideAgentEnrollment(
  interactionId: string,
  body: EnrollmentDecisionRequest
): Promise<EnrollmentDecisionResponse> {
  const { data, error } = await api.POST("/api/agent-enrollment/{interaction_id}/decision", {
    params: { path: { interaction_id: interactionId } },
    body,
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to complete Agent enrollment"));
  return data;
}

// stays server-side; this only triggers the action and returns the new session URL.
export async function launchRoutine(text?: string): Promise<LaunchRoutineResult> {
  const { data, error } = await api.POST("/api/capabilities/launch-routine", {
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

/** One page of the ledger, plus the position to resume from (null once the page is the last). */
export interface ToolCallPage {
  records: ToolCallRecord[];
  nextCursor: string | null;
}

// The tool-call audit ledger for the history view: newest first, one page at a time. A record
// carries its whole arguments and result payload — megabytes for a few hundred of them — so the
// page follows `next_cursor` instead of asking for the ledger's cap up front.
// `showAutoApproved` false asks the server to filter out auto-approved calls (rather than
// over-fetching and discarding client-side, which would starve the page of older manual calls
// once auto-approved traffic fills the window).
export async function fetchToolCalls(
  limit: number,
  showAutoApproved: boolean,
  cursor: string | null = null
): Promise<ToolCallPage> {
  const { data, error } = await api.GET("/api/tool-calls", {
    params: {
      query: {
        newest_first: true,
        limit,
        auto_approved: showAutoApproved ? undefined : false,
        cursor: cursor ?? undefined,
      },
    },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load tool calls"));
  return { records: data.tool_calls ?? [], nextCursor: data.next_cursor ?? null };
}

export async function connectMcpOperatorAuth(serverId: string): Promise<McpOperatorAuthConnectResponse> {
  const { data, error } = await api.POST("/api/mcp/operator-auth/{server_id}/connect", {
    params: { path: { server_id: serverId } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to start MCP account link"));
  return data;
}

export async function disconnectMcpOperatorAuth(serverId: string): Promise<McpOperatorAuthStatus> {
  const { data, error } = await api.DELETE("/api/mcp/operator-auth/{server_id}", {
    params: { path: { server_id: serverId } },
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
  const { data, error } = await api.POST("/api/operator-connections/{connection}/connect", {
    params: { path: { connection } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to start account connection"));
  return data;
}

export async function disconnectOperatorConnection(
  connection: OperatorConnectionName
): Promise<components["schemas"]["ProviderUnconnected"]> {
  const { data, error } = await api.DELETE("/api/operator-connections/{connection}", {
    params: { path: { connection } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to disconnect account"));
  return data;
}

async function decideToolCall(
  toolCallId: string,
  body: ApprovalDecisionRequest,
  fallback: string
): Promise<ToolCallRecord> {
  const { data, error } = await api.POST("/api/tool-calls/{tool_call_id}/decision", {
    params: { path: { tool_call_id: toolCallId } },
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
