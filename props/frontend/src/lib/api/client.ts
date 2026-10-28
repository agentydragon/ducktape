import createClient from "openapi-fetch";
import type {
  AgentRunDetail,
  AgentRunStatus,
  AgentType,
  ClustersListResponse,
  CriticRunSpecifics,
  ExampleKind,
  FileContentResponse,
  FileTreeResponse,
  GraderRunSpecifics,
  RunSortDirection,
  RunSortKey,
  SnapshotDetailResponse,
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

// Authenticated fetch for endpoints with slash-containing path params (snapshot slugs).
// openapi-fetch encodes '/' in path params, breaking FastAPI's {slug:path} routes.
async function authedFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers = new Headers(init?.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const resp = await fetch(url, {
    ...init,
    credentials: "include",
    headers,
  });
  if (resp.status === 401) onAuthFailed();
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const msg = body?.detail ?? `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return resp.json();
}

// Type guards for AgentRunDetail discriminated union
export function isCriticRun(run: AgentRunDetail): run is AgentRunDetail & { details: CriticRunSpecifics } {
  return "agent_type" in run.details && run.details.agent_type === "critic";
}

export function isGraderRun(run: AgentRunDetail): run is AgentRunDetail & { details: GraderRunSpecifics } {
  return "agent_type" in run.details && run.details.agent_type === "grader";
}

// Enum value arrays for UI dropdowns (must match schema definitions)
export const AGENT_RUN_STATUS_VALUES: AgentRunStatus[] = [
  "in_progress",
  "exited",
  "timed_out",
  "cancelled",
  "startup_failed",
];

export const AGENT_TYPE_VALUES: AgentType[] = [
  "critic",
  "grader",
  "critic_dev_optimize",
  "critic_dev_improve",
  "freeform",
];

// Extract error message from API error response
function extractErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === "object") {
    // FastAPI HTTPException format: { detail: string }
    if ("detail" in error && typeof (error as { detail: unknown }).detail === "string") {
      return (error as { detail: string }).detail;
    }
    // Generic message field
    if ("message" in error && typeof (error as { message: unknown }).message === "string") {
      return (error as { message: string }).message;
    }
  }
  return fallback;
}

type ApiSuccessData<Result> = Extract<Result, { data: unknown }> extends { data: infer Data } ? Data : never;

function unwrapApiResult<Result extends { data?: unknown; error?: unknown }>(
  result: Result,
  fallback: string
): ApiSuccessData<Result> {
  if ("error" in result) throw new Error(extractErrorMessage(result.error, fallback));
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

// Fetch all runs with filters and pagination
export interface RunsFilters {
  status?: AgentRunStatus;
  image_digest?: string;
  agent_type?: AgentType;
  split?: Split;
  example_kind?: ExampleKind;
  offset?: number;
  limit?: number;
  sort_by?: RunSortKey;
  sort_dir?: RunSortDirection;
}

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

// --- Ground truth snapshots ---
export async function fetchSnapshots() {
  const result = await api.GET("/api/gt/snapshots");
  return unwrapApiResult(result, "Failed to fetch snapshots");
}

export async function fetchSnapshotDetail(
  snapshotSlug: string,
  exampleKind?: string,
  filesHash?: string,
  signal?: AbortSignal
): Promise<SnapshotDetailResponse> {
  const params = new URLSearchParams();
  if (exampleKind) params.set("example_kind", exampleKind);
  if (filesHash) params.set("files_hash", filesHash);
  const qs = params.toString();
  return authedFetch(`/api/gt/snapshots/${snapshotSlug}${qs ? "?" + qs : ""}`, { signal });
}

// File tree types (autogenerated from OpenAPI schema)
export type FileContentsResponse = {
  files: FileContentResponse[];
  missing: string[];
};

// Fetch snapshot file tree
export async function fetchSnapshotTree(snapshotSlug: string): Promise<FileTreeResponse> {
  return authedFetch(`/api/gt/snapshots/${snapshotSlug}/tree`);
}

// Fetch file content from snapshot
export async function fetchSnapshotFile(snapshotSlug: string, filePath: string): Promise<FileContentResponse> {
  return authedFetch(`/api/gt/snapshots/${snapshotSlug}/files/${filePath}`);
}

// Fetch multiple file contents from a snapshot with one backend tar read.
export async function fetchSnapshotFiles(
  snapshotSlug: string,
  paths: string[],
  signal?: AbortSignal
): Promise<FileContentsResponse> {
  return authedFetch(`/api/gt/snapshots/${snapshotSlug}/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
    signal,
  });
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

// --- Clusters ---
export async function fetchSnapshotClusters(snapshotSlug: string): Promise<ClustersListResponse> {
  return authedFetch(`/api/gt/snapshots/${snapshotSlug}/clusters`);
}

// --- Model metadata ---

export async function fetchModelMetadata() {
  const result = await api.GET("/api/model_metadata");
  return unwrapApiResult(result, "Failed to fetch model metadata");
}

// --- Stats: occurrence stats, distributions, coverage ---

export async function fetchOccurrenceStats(snapshotSlug?: string, split?: Split) {
  const result = await api.GET("/api/stats/occurrences", {
    params: {
      query: {
        limit: 500,
        sort_by: "mean_credit",
        sort_dir: "asc",
        ...(snapshotSlug ? { snapshot_slug: snapshotSlug } : {}),
        ...(split ? { split } : {}),
      },
    },
  });
  return unwrapApiResult(result, "Failed to fetch occurrence stats");
}

export async function fetchCoverage(split: Split, limitDefinitions?: number) {
  const result = await api.GET("/api/stats/coverage", {
    params: { query: { split, ...(limitDefinitions ? { limit_definitions: limitDefinitions } : {}) } },
  });
  return unwrapApiResult(result, "Failed to fetch coverage");
}
