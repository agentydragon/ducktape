import createClient, { defaultPathSerializer } from "openapi-fetch";
import type {
  AgentRunDetail,
  AgentType,
  CriticRunSpecifics,
  ExampleKind,
  GraderRunSpecifics,
  Split,
  ValidationRunRequest,
  paths,
} from "../schema";
import { getToken, onAuthFailed } from "$lib/stores/token";

// Create typed API client (types from Bazel: //props/frontend/src/lib:schema).
// `credentials: "include"` sends the Authentik SSO session cookie (props_session)
// so browser users authenticated via /auth/login don't need a token.
export const api = createClient<paths>({ baseUrl: "", credentials: "include" });

// Attach the admin token as a Bearer header when one is stored (token fallback).
// Without a token, the session cookie above carries authentication.
api.use({
  async onRequest({ request }) {
    const token = getToken();
    if (token) {
      request.headers.set("Authorization", `Bearer ${token}`);
    }
    return request;
  },
  async onResponse({ response }) {
    if (response.status === 401) {
      onAuthFailed();
    }
    return response;
  },
});

// Type guards for AgentRunDetail discriminated union
export function isCriticRun(run: AgentRunDetail): run is AgentRunDetail & { details: CriticRunSpecifics } {
  return "agent_type" in run.details && run.details.agent_type === "critic";
}

export function isGraderRun(run: AgentRunDetail): run is AgentRunDetail & { details: GraderRunSpecifics } {
  return "agent_type" in run.details && run.details.agent_type === "grader";
}

// Extract error message from API error response
function extractErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === "object") {
    // FastAPI HTTPException format: { detail: string }
    if ("detail" in error && typeof error.detail === "string") {
      return error.detail;
    }
    // Generic message field
    if ("message" in error && typeof error.message === "string") {
      return error.message;
    }
  }
  return fallback;
}

type ApiSuccessData<Result> = Extract<Result, { data: unknown }> extends { data: infer Data } ? Data : never;

function unwrapApiResult<Result extends { data?: unknown; error?: unknown; response: Response }>(
  result: Result,
  fallback?: string
): ApiSuccessData<Result> {
  if ("error" in result)
    throw new Error(extractErrorMessage(result.error, fallback ?? `HTTP ${result.response.status}`));
  return result.data as ApiSuccessData<Result>;
}

// Convenience wrapper for overview endpoint
export async function fetchOverview() {
  const result = await api.GET("/api/stats/overview");
  return unwrapApiResult(result, "Failed to fetch overview");
}

// Fetch all definitions
export async function fetchDefinitions(agentType?: AgentType) {
  const result = await api.GET("/api/definitions", {
    params: { query: agentType ? { agent_type: agentType } : {} },
  });
  return unwrapApiResult(result, "Failed to fetch definitions");
}

// Trigger validation runs
export async function triggerValidationRuns(request: ValidationRunRequest) {
  const result = await api.POST("/api/runs/validation", {
    body: request,
  });
  return unwrapApiResult(result, "Failed to trigger validation runs");
}

// Fetch run details
export async function fetchRun(runId: string, signal?: AbortSignal) {
  const result = await api.GET("/api/runs/{run_id}", {
    params: { path: { run_id: runId } },
    signal,
  });
  return unwrapApiResult(result, "Failed to fetch run");
}

export type RunsFilters = NonNullable<paths["/api/runs"]["get"]["parameters"]["query"]>;

export async function fetchRuns(filters?: RunsFilters) {
  const result = await api.GET("/api/runs", {
    params: { query: filters ?? {} },
  });
  return unwrapApiResult(result, "Failed to fetch runs");
}

// Fetch definition detail with per-example stats
export async function fetchDefinitionDetail(definitionId: string) {
  const result = await api.GET("/api/stats/definitions/{image_digest}", {
    params: { path: { image_digest: definitionId } },
  });
  return unwrapApiResult(result, "Failed to fetch definition");
}

// Fetch example detail with per-definition stats
export async function fetchExampleDetail(snapshotSlug: string, exampleKind: ExampleKind, filesHash: string | null) {
  const result = await api.GET("/api/stats/examples", {
    params: {
      query: {
        snapshot_slug: snapshotSlug,
        example_kind: exampleKind,
        ...(filesHash ? { files_hash: filesHash } : {}),
      },
    },
  });
  return unwrapApiResult(result, "Failed to fetch example detail");
}

export async function fetchSnapshots() {
  const result = await api.GET("/api/gt/snapshots");
  return unwrapApiResult(result, "Failed to fetch snapshots");
}

function snapshotPath(slug: string) {
  const [org, snapshot_date, extra] = slug.split("/");
  if (!org || !snapshot_date || extra !== undefined) throw new Error(`Invalid snapshot slug: ${slug}`);
  return { org, snapshot_date };
}

export async function fetchSnapshotDetail(
  snapshotSlug: string,
  exampleKind?: string,
  filesHash?: string,
  signal?: AbortSignal
) {
  const result = await api.GET("/api/gt/snapshots/{org}/{snapshot_date}", {
    params: {
      path: snapshotPath(snapshotSlug),
      query: { example_kind: exampleKind || undefined, files_hash: filesHash || undefined },
    },
    signal,
  });
  return unwrapApiResult(result);
}

export async function fetchSnapshotTree(snapshotSlug: string, signal?: AbortSignal) {
  const result = await api.GET("/api/gt/snapshots/{org}/{snapshot_date}/tree", {
    params: { path: snapshotPath(snapshotSlug) },
    signal,
  });
  return unwrapApiResult(result);
}

export async function fetchSnapshotFile(snapshotSlug: string, filePath: string, signal?: AbortSignal) {
  const result = await api.GET("/api/gt/snapshots/{org}/{snapshot_date}/files/{file_path}", {
    params: { path: { ...snapshotPath(snapshotSlug), file_path: filePath } },
    // FastAPI's file_path:path spans segments. Preserve its separators after escaping the values.
    pathSerializer: (template, params) => defaultPathSerializer(template, params).replaceAll("%2F", "/"),
    signal,
  });
  return unwrapApiResult(result);
}

export async function fetchSnapshotFiles(snapshotSlug: string, paths: string[], signal?: AbortSignal) {
  const result = await api.POST("/api/gt/snapshots/{org}/{snapshot_date}/files", {
    params: { path: snapshotPath(snapshotSlug) },
    body: { paths },
    signal,
  });
  return unwrapApiResult(result);
}

// Fetch LLM requests for an agent run
export async function fetchLLMRequests(runId: string, signal?: AbortSignal) {
  const result = await api.GET("/api/runs/{run_id}/llm_requests", {
    params: { path: { run_id: runId } },
    signal,
  });
  return unwrapApiResult(result, "Failed to fetch LLM requests");
}

// Fetch container logs (from Loki) for an agent run
export async function fetchRunLogs(runId: string, signal?: AbortSignal) {
  const result = await api.GET("/api/runs/{run_id}/logs", {
    params: { path: { run_id: runId } },
    signal,
  });
  return unwrapApiResult(result, "Failed to fetch logs");
}

export async function fetchSnapshotClusters(snapshotSlug: string, signal?: AbortSignal) {
  const result = await api.GET("/api/gt/snapshots/{org}/{snapshot_date}/clusters", {
    params: { path: snapshotPath(snapshotSlug) },
    signal,
  });
  return unwrapApiResult(result);
}

export async function fetchModelMetadata() {
  const result = await api.GET("/api/model_metadata");
  return unwrapApiResult(result, "Failed to fetch model metadata");
}

export async function fetchOccurrenceStats(snapshotSlug?: string, signal?: AbortSignal) {
  const result = await api.GET("/api/stats/occurrences", {
    params: {
      query: {
        limit: 500,
        sort_by: "mean_credit",
        sort_dir: "asc",
        ...(snapshotSlug ? { snapshot_slug: snapshotSlug } : {}),
      },
    },
    signal,
  });
  return unwrapApiResult(result, "Failed to fetch occurrence stats");
}

export async function fetchCoverage(split: Split) {
  const result = await api.GET("/api/stats/coverage", { params: { query: { split } } });
  return unwrapApiResult(result, "Failed to fetch coverage");
}
