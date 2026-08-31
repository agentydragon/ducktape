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
export type LaunchOption = components["schemas"]["LaunchOption"];
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
// The one conversation item-read vocabulary, shared with the MCP conversation reads: one item per
// row, keyed by the position it opened at, its lifecycle carried as `status`.
export type Item = components["schemas"]["Item"];
export type QueuedPrompt = components["schemas"]["QueuedPrompt"];
export type ToolCallItem = components["schemas"]["ToolCallItem"];
export type ConversationSummary = components["schemas"]["ConversationSummary"];
export type ConversationPage = components["schemas"]["ConversationPage"];
export type ConversationCursor = components["schemas"]["ConversationCursor"];
export type Conversation = components["schemas"]["ConversationView"];
export type Session = components["schemas"]["SessionView"];
// What `WS /api/conversations/{id}/follow` sends. Generated like every type above: the schema
// carries these components because the exporter publishes them (//haku/console:export_schema),
// a WebSocket having no route for FastAPI to document.
export type ConversationFollowMessage = components["schemas"]["ConversationFollowMessage"];
export type ConversationUpdate = components["schemas"]["ConversationUpdate"];
export type SessionFrame = components["schemas"]["SessionFrameView"];
export type SessionFramePage = components["schemas"]["SessionFramePage"];
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

/** Mint a Web conversation with its explicit deploy-authorized Agent/harness pair. */
export async function createConversation(selection: LaunchOption): Promise<Conversation> {
  const response = await api.POST("/api/conversations", {
    body: { agent_id: selection.agent_id, harness_kind: selection.harness_kind },
  });
  const { data, error } = response;
  if (error || !data) throw new Error(errorDetail(error, "Failed to start a conversation"));
  return data;
}

/** One page of conversations, newest activity first.
 *
 * `cursor` is a previous page's `next_cursor`; omitting it opens on the newest. Keyset rather than
 * an offset because a conversation never ends, so this list only grows and only at its top.
 */
export async function fetchConversations(cursor?: ConversationCursor, limit = 25): Promise<ConversationPage> {
  const { data, error } = await api.GET("/api/conversations", {
    params: {
      query: { limit, before_activity: cursor?.last_activity_at, before_conversation: cursor?.conversation_id },
    },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load conversations"));
  return data;
}

/** One page of a conversation's raw protocol frames, in wire order.
 *
 * Omitting `beforeSeq` reads the *tail* of the log; the response's `next_before_seq` walks back
 * from there. Every native frame is returned verbatim without a generic discriminator or filter.
 */
export async function fetchSessionFrames(
  sessionId: string,
  limit: number,
  beforeSeq?: number
): Promise<SessionFramePage> {
  const { data, error } = await api.GET("/api/sessions/{session_id}/frames", {
    params: { path: { session_id: sessionId }, query: { limit, before_seq: beforeSeq } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load session frames"));
  return data;
}

/** The console would not take the prompt, and recorded nothing.
 *
 * `SessionStore.enqueue_prompt` refuses a session that is not `ready`, one whose turn is still in
 * flight, and one that already has a prompt queued; it holds none of them. Distinct from a
 * transport failure because the operator's text still exists only in their composer — a caller
 * that catches this must keep it.
 */
export class PromptRefused extends Error {}

export async function sendChatPrompt(conversationId: string, text: string): Promise<void> {
  const { data, error, response } = await api.POST("/api/conversations/{conversation_id}/messages", {
    params: { path: { conversation_id: conversationId } },
    body: { text },
  });
  if (response.status === 409) throw new PromptRefused(errorDetail(error, "The session would not take that prompt"));
  if (error || !data) throw new Error(errorDetail(error, "Failed to send the prompt"));
  // The prompt's own rows arrive over the conversation's follow socket, where every other surface's
  // prompts arrive too, so there is nothing to hand back that is not already on its way.
}

/** Interrupt the running turn; false when the console found none open.
 *
 * Not an error: the turn can end between the operator seeing the button and pressing it, and
 * "there was nothing left to stop" is the outcome they wanted either way.
 */
export async function abortSessionTurn(sessionId: string): Promise<string | false> {
  const { data, error, response } = await api.POST("/api/sessions/{session_id}/abort", {
    params: { path: { session_id: sessionId } },
  });
  if (response.status === 409) return false;
  if (error || !data) throw new Error(errorDetail(error, "Failed to abort the turn"));
  return data.status;
}

/** End this session and release its sandbox. The conversation it ran outlives it. */
export async function closeSession(sessionId: string): Promise<void> {
  const { error } = await api.DELETE("/api/sessions/{session_id}", {
    params: { path: { session_id: sessionId } },
  });
  if (error) throw new Error(errorDetail(error, "Failed to close the session"));
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
