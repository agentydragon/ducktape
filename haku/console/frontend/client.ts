import createClient from "openapi-fetch";

import type { components, paths } from "./api/schema";
import { operatorLoginRedirectStarted, redirectToOperatorLogin } from "./operator_login";

// Same-origin typed client (nginx serves this bundle and proxies /api). Types are generated from
// the backend's OpenAPI schema: //haku/console/frontend:schema. Exported so the per-integration
// client files (gmail_client.ts, grocy_client.ts) share this one instance.
export const api: ReturnType<typeof createClient<paths>> = createClient<paths>({ baseUrl: "" });

// App-owned operator auth: the /api/* guards answer 401 when there is no operator session, while
// the SPA itself is served publicly — so a 401 bounces the browser to /auth/login, where Authentik's
// application access policy decides who may complete it. With operator_oidc unset (dev/test) the
// guards no-op and /api never 401s, so this never fires there.
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
  | components["schemas"]["ConnectionSucceeded"]
  | components["schemas"]["ConnectionFailed"];
export type AgentView = components["schemas"]["AgentView"];
export type AgentListResponse = components["schemas"]["AgentListResponse"];
export type Grant = components["schemas"]["Grant"];
export type GrantPrincipal = Grant["subject"];
export type GrantListResponse = components["schemas"]["GrantListResponse"];
export type RevokeGrantResponse = components["schemas"]["RevokeGrantResponse"];
export type EnrollmentView = components["schemas"]["EnrollmentView"];
export type EnrollmentDecisionRequest =
  | components["schemas"]["CreateEnrollmentRequest"]
  | components["schemas"]["ReconnectEnrollmentRequest"]
  | components["schemas"]["DenyEnrollmentRequest"];
export type EnrollmentDecisionResponse =
  | components["schemas"]["EnrollmentContinues"]
  | components["schemas"]["EnrollmentWasDenied"];
export type AiquotaView = components["schemas"]["AllQuotasView"];

// FastAPI error responses are `{detail: string}`; surface that real reason, falling back when the
// body isn't shaped that way. Shared with the per-integration client files.
export function errorDetail(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "detail" in error) {
    const { detail } = error as { detail: unknown };
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

// Null means "nothing to show", which is the right answer once a 401 has started the login redirect
// (the middleware above): this document is about to be replaced, so reporting the failure of the
// request that triggered the redirect only flashes a detail string at an operator being signed
// straight back in.
export function displayableError(e: unknown): string | null {
  if (operatorLoginRedirectStarted()) return null;
  return e instanceof Error ? e.message : String(e);
}

export async function fetchConfig(): Promise<ConfigResponse> {
  const { data, error } = await api.GET("/api/config");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load config"));
  return data;
}

export async function fetchAiquotaQuotas(): Promise<AiquotaView> {
  const { data, error } = await api.GET("/api/aiquota/quotas");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load aiquota"));
  return data;
}

/** The signed-in operator and the absolute deadline their session stops being accepted at. */
export async function fetchOperator(): Promise<OperatorResponse> {
  const { data, error } = await api.GET("/auth/me");
  if (error || !data) throw new Error(errorDetail(error, "Failed to load the operator session"));
  return data;
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

export async function updateAgentAccessProfile(agentId: string, accessProfileId: string): Promise<AgentView> {
  const { data, error } = await api.PUT("/api/agent-enrollment/agents/{agent_id}/access-profile", {
    params: { path: { agent_id: agentId } },
    body: { access_profile_id: accessProfileId },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to update Agent access profile"));
  return data;
}

/** List every grant, or only grants declared for one exact principal. */
export async function fetchGrants(principal?: GrantPrincipal): Promise<GrantListResponse> {
  const { data, error } = await api.GET("/api/grants", {
    params: { query: principal ? { principal: JSON.stringify(principal) } : {} },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load grants"));
  return data;
}

export async function revokeGrant(grantId: string): Promise<RevokeGrantResponse> {
  const { data, error } = await api.POST("/api/grants/revoke", {
    body: { grant_ids: [grantId] },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to revoke grant"));
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

export async function fetchToolCall(toolCallId: string): Promise<ToolCallRecord> {
  const { data, error } = await api.GET("/api/tool-calls/{tool_call_id}", {
    params: { path: { tool_call_id: toolCallId } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load tool call"));
  return data;
}

/** One page of the ledger, plus the position to resume from (null once the page is the last). */
export interface ToolCallPage {
  records: ToolCallRecord[];
  nextCursor: string | null;
}

// The tool-call audit ledger for the history view: newest first, one page at a time. A record
// carries its whole arguments and result payload — megabytes for a few hundred of them — so the
// page follows `next_cursor` instead of asking for the ledger's cap up front. `showAutoApproved`
// false filters server-side; discarding client-side would starve the page of older manual calls
// once auto-approved traffic fills the window.
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

// Per-Operator external account connections (Google today). Connect opens the provider's consent in
// a new tab; the backend callback stores the refresh token and broadcasts an
// `operator_connection_changed` event.
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

export async function approveToolCall(toolCallId: string, decisionNote?: string): Promise<ToolCallRecord> {
  return decideToolCall(
    toolCallId,
    { decision: "approve", decision_note: decisionNote ?? null },
    "Failed to approve tool call"
  );
}

export async function denyToolCall(toolCallId: string, decisionNote?: string): Promise<ToolCallRecord> {
  return decideToolCall(
    toolCallId,
    { decision: "deny", decision_note: decisionNote ?? null },
    "Failed to deny tool call"
  );
}
