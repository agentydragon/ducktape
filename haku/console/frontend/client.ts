import createClient from "openapi-fetch";

import type {
  AgentListResponse,
  AgentView,
  ConfigResponse,
  DeploymentInfo,
  LaunchRoutineResult,
  OperatorResponse,
  paths,
} from "./schema";
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

export async function launchRoutine(text?: string): Promise<LaunchRoutineResult> {
  const { data, error } = await api.POST("/api/capabilities/launch-routine", {
    body: text ? { text } : {},
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to launch routine"));
  return data;
}
