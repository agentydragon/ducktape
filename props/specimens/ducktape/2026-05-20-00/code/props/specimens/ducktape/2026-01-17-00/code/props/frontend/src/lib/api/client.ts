import createClient from "openapi-fetch";
import type { paths, components } from "./schema";

// Create typed API client (types from Bazel: //props/frontend:generate_schema)
export const api = createClient<paths>({ baseUrl: "" });

// Re-export generated types
export type DefinitionInfo = components["schemas"]["DefinitionInfo"];
export type ActiveRunInfo = components["schemas"]["ActiveRunInfo"];
export type ValidationRunRequest = components["schemas"]["ValidationRunRequest"];
export type ValidationRunResponse = components["schemas"]["ValidationRunResponse"];
export type CriticRunSpecifics = components["schemas"]["CriticRunSpecifics"];
export type GraderRunSpecifics = components["schemas"]["GraderRunSpecifics"];
export type OtherRunSpecifics = components["schemas"]["OtherRunSpecifics"];
export type AgentRunDetail = components["schemas"]["AgentRunDetail"];

// Type guards for AgentRunDetail discriminated union
export function isCriticRun(run: AgentRunDetail): run is AgentRunDetail & { details: CriticRunSpecifics } {
  return "agent_type" in run.details && run.details.agent_type === "critic";
}

export function isGraderRun(run: AgentRunDetail): run is AgentRunDetail & { details: GraderRunSpecifics } {
  return "agent_type" in run.details && run.details.agent_type === "grader";
}

export type EventInfo = components["schemas"]["ParsedEventInfo"];
export type EventsResponse = components["schemas"]["EventsResponse"];
export type AgentRunStatus = components["schemas"]["AgentRunStatus"];
export type JobInfo = components["schemas"]["JobInfo"];
export type JobsResponse = components["schemas"]["JobsResponse"];
export type AgentType = components["schemas"]["AgentType"];
export type RunInfo = components["schemas"]["RunInfo"];
export type RunsListResponse = components["schemas"]["RunsListResponse"];
export type CriticTypeConfig = components["schemas"]["CriticTypeConfig"];
export type GraderTypeConfig = components["schemas"]["GraderTypeConfig"];
export type FreeformTypeConfig = components["schemas"]["FreeformTypeConfig"];
export type PromptOptimizerTypeConfig = components["schemas"]["PromptOptimizerTypeConfig"];
export type ImprovementTypeConfig = components["schemas"]["ImprovementTypeConfig"];
export type WholeSnapshotExample = components["schemas"]["WholeSnapshotExample"];
export type SingleFileSetExample = components["schemas"]["SingleFileSetExample"];
export type Split = components["schemas"]["Split"];
export type ExampleKind = components["schemas"]["ExampleKind"];
export type ChildRunInfo = components["schemas"]["ChildRunInfo"];
export type GraderRunInfo = components["schemas"]["GraderRunInfo"];
export type GradingEdgeInfo = components["schemas"]["GradingEdgeInfo"];
export type TpTarget = components["schemas"]["TpTarget"];
export type FpTarget = components["schemas"]["FpTarget"];
export type GradingTarget = TpTarget | FpTarget;

// Event payload types (discriminated union)
export type DockerExecCallPayload = components["schemas"]["DockerExecCallPayload"];
export type DockerExecOutputPayload = components["schemas"]["DockerExecOutputPayload"];
export type GenericToolCallPayload = components["schemas"]["GenericToolCallPayload"];
export type GenericToolOutputPayload = components["schemas"]["GenericToolOutputPayload"];
export type UserText = components["schemas"]["UserText"];
export type AssistantText = components["schemas"]["AssistantText"];
export type ApiRequest = components["schemas"]["ApiRequest"];
export type Response = components["schemas"]["Response"];
export type ReasoningItem = components["schemas"]["ReasoningItem"];

// Docker exec types (from mcp_infra.exec.models)
export type ExecInput = components["schemas"]["ExecInput"];
export type BaseExecResult = components["schemas"]["BaseExecResult"];
export type TruncatedStream = components["schemas"]["TruncatedStream"];
export type Exited = components["schemas"]["Exited"];
export type TimedOut = components["schemas"]["TimedOut"];
export type Killed = components["schemas"]["Killed"];
export type ExitStatus = Exited | TimedOut | Killed;

// Enum value arrays for UI dropdowns (must match schema definitions)
export const AGENT_RUN_STATUS_VALUES: AgentRunStatus[] = [
  "in_progress",
  "completed",
  "max_turns_exceeded",
  "context_length_exceeded",
  "reported_failure",
];

export const AGENT_TYPE_VALUES: AgentType[] = ["critic", "grader", "prompt_optimizer", "improvement", "freeform"];

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

// Convenience wrapper for overview endpoint
export async function fetchOverview() {
  const { data, error } = await api.GET("/api/stats/overview");
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch overview"));
  return data;
}

// Fetch all definitions
export async function fetchDefinitions(agentType?: AgentType) {
  const { data, error } = await api.GET("/api/stats/definitions", {
    params: { query: agentType ? { agent_type: agentType } : {} },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch definitions"));
  return data;
}

// Fetch active runs
export async function fetchActiveRuns() {
  const { data, error } = await api.GET("/api/runs/active");
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch active runs"));
  return data;
}

// Trigger validation runs
export async function triggerValidationRuns(request: ValidationRunRequest) {
  const { data, error } = await api.POST("/api/runs/validation", {
    body: request,
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to trigger validation runs"));
  return data;
}

// Fetch validation jobs
export async function fetchJobs() {
  const { data, error } = await api.GET("/api/runs/jobs");
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch jobs"));
  return data;
}

// Fetch run details
export async function fetchRun(runId: string) {
  const { data, error } = await api.GET("/api/runs/run/{run_id}", {
    params: { path: { run_id: runId } },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch run"));
  return data;
}

// Fetch run events
export async function fetchRunEvents(runId: string, offset = 0, limit = 100) {
  const { data, error } = await api.GET("/api/runs/run/{run_id}/events", {
    params: { path: { run_id: runId }, query: { offset, limit } },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch events"));
  return data;
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
}

export async function fetchRuns(filters?: RunsFilters) {
  const { data, error } = await api.GET("/api/runs", {
    params: { query: filters ?? {} },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch runs"));
  return data;
}

// Fetch definition detail with per-example stats
export type DefinitionDetailResponse = components["schemas"]["DefinitionDetailResponse"];
export type ExampleStats = components["schemas"]["ExampleStats"];

export async function fetchDefinitionDetail(definitionId: string) {
  const { data, error } = await api.GET("/api/stats/definitions/{image_digest}", {
    params: { path: { image_digest: definitionId } },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch definition"));
  return data;
}

// Fetch example detail with per-definition stats
export type ExampleDetailResponse = components["schemas"]["ExampleDetailResponse"];
export type DefinitionStatsForExample = components["schemas"]["DefinitionStatsForExample"];

export async function fetchExampleDetail(snapshotSlug: string, exampleKind: ExampleKind, filesHash: string | null) {
  const { data, error } = await api.GET("/api/stats/examples", {
    params: {
      query: {
        snapshot_slug: snapshotSlug,
        example_kind: exampleKind,
        ...(filesHash ? { files_hash: filesHash } : {}),
      },
    },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch example detail"));
  return data;
}

// --- Overview ---
export type OverviewResponse = components["schemas"]["OverviewResponse"];

// --- Ground truth snapshots ---
export type SnapshotsResponse = components["schemas"]["SnapshotsListResponse"];
export type SnapshotSummary = components["schemas"]["SnapshotSummary"];
export type SnapshotDetailResponse = components["schemas"]["SnapshotDetailResponse"];
export type TpInfo = components["schemas"]["TpInfo"];
export type FpInfo = components["schemas"]["FpInfo"];
export type OccurrenceInfo = components["schemas"]["OccurrenceInfo"];
export type ReportedIssueInfo = components["schemas"]["ReportedIssueInfo"];
export type ReportedIssueOccurrenceInfo = components["schemas"]["ReportedIssueOccurrenceInfo"];
export type FileLocationInfo = components["schemas"]["FileLocationInfo"];
export type LineRange = components["schemas"]["LineRange"];

export async function fetchSnapshots() {
  const { data, error } = await api.GET("/api/gt/snapshots");
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch snapshots"));
  return data;
}

export async function fetchSnapshotDetail(snapshotSlug: string) {
  const { data, error } = await api.GET("/api/gt/snapshots/{snapshot_slug}", {
    params: { path: { snapshot_slug: snapshotSlug } },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch snapshot"));
  return data;
}

// File tree types (autogenerated from OpenAPI schema)
export type FileTreeNode = components["schemas"]["FileTreeNode"];
export type FileTreeResponse = components["schemas"]["FileTreeResponse"];
export type FileContentResponse = components["schemas"]["FileContentResponse"];

// Fetch snapshot file tree
export async function fetchSnapshotTree(snapshotSlug: string) {
  const { data, error } = await api.GET("/api/gt/snapshots/{snapshot_slug}/tree", {
    params: { path: { snapshot_slug: snapshotSlug } },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch snapshot tree"));
  return data;
}

// Fetch file content from snapshot
export async function fetchSnapshotFile(snapshotSlug: string, filePath: string) {
  const { data, error } = await api.GET("/api/gt/snapshots/{snapshot_slug}/files/{file_path}", {
    params: { path: { snapshot_slug: snapshotSlug, file_path: filePath } },
  });
  if (error) throw new Error(extractErrorMessage(error, "Failed to fetch file"));
  return data;
}
