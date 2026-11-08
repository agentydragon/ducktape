// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { deferred } from "../../../../util/frontend/testing/deferred";
import type { AgentRunDetail, LlmRequestInfo, SnapshotDetailResponse } from "../lib/schema";
import { fetchLLMRequests, fetchRun, fetchRunLogs, fetchSnapshotDetail, fetchSnapshotFiles } from "../lib/api/client";
import { toast } from "../lib/toast";
import { createPropsQueryClient } from "../lib/queryClient";
import RunDetail from "./RunDetail";

vi.mock("../lib/api/client", () => ({
  fetchLLMRequests: vi.fn(),
  fetchRun: vi.fn(),
  fetchRunLogs: vi.fn(),
  fetchSnapshotDetail: vi.fn(),
  fetchSnapshotFiles: vi.fn(),
  isCriticRun: (run: { details: { agent_type?: string } }) => run.details.agent_type === "critic",
  isGraderRun: (run: { details: { agent_type?: string } }) => run.details.agent_type === "grader",
}));

vi.mock("../lib/toast", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type RunResponse = Awaited<ReturnType<typeof fetchRun>>;
type SnapshotResponse = Awaited<ReturnType<typeof fetchSnapshotDetail>>;
type LLMResponse = Awaited<ReturnType<typeof fetchLLMRequests>>;

function run(runId: string, snapshotSlug?: string, filesHash?: string): AgentRunDetail {
  const critic = snapshotSlug !== undefined;
  return {
    agent_run_id: runId,
    image_digest: "sha256:test-image",
    model: `model-for-${runId}`,
    status: "exited",
    container_exit_code: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    llm_call_count: 1,
    budget_usd: 0,
    parent_agent_run_id: null,
    llm_costs: null,
    type_config: critic
      ? {
          agent_type: "critic",
          example:
            filesHash === undefined
              ? { kind: "whole_snapshot", snapshot_slug: snapshotSlug }
              : { kind: "file_set", snapshot_slug: snapshotSlug, files_hash: filesHash },
        }
      : { agent_type: "freeform" },
    details: critic
      ? {
          agent_type: "critic",
          reported_issues: [
            {
              issue_id: `issue-${runId}`,
              rationale: "The request belongs to the selected run.",
              occurrences: [
                {
                  occurrence_id: 1,
                  note: null,
                  locations: [{ file: `${runId}-source.ts`, start_line: null, end_line: null, note: null }],
                },
              ],
            },
          ],
          resolved_files: [],
          grader_runs: [],
        }
      : { agent_type: "freeform" },
    child_runs: [],
  } satisfies AgentRunDetail;
}

function llmRequest(model: string): LlmRequestInfo {
  return {
    id: 1,
    model,
    api_shape: "chat_completions",
    request_body: {},
    response_body: null,
    response_error_body: null,
    error: null,
    latency_ms: null,
    created_at: "2026-01-01T00:00:00Z",
  } satisfies LlmRequestInfo;
}

function llmResponse(model: string): LLMResponse {
  return { requests: [llmRequest(model)] };
}

function snapshotResponse(slug = "test/snapshot"): SnapshotResponse {
  return {
    slug,
    split: "valid",
    created_at: "2026-01-01T00:00:00Z",
    true_positives: [],
    false_positives: [],
  } satisfies SnapshotDetailResponse;
}

let root: Root | null = null;
let queryClient: ReturnType<typeof createPropsQueryClient> | null = null;
let container: HTMLDivElement;

async function mount(runId: string): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const client = createPropsQueryClient();
  queryClient = client;
  await act(async () => {
    root?.render(
      <QueryClientProvider client={client}>
        <MantineProvider env="test">
          <MemoryRouter>
            <RunDetail runId={runId} />
          </MemoryRouter>
        </MantineProvider>
      </QueryClientProvider>
    );
  });
}

async function switchRun(runId: string): Promise<void> {
  await act(async () => {
    root?.render(
      <QueryClientProvider client={queryClient!}>
        <MantineProvider env="test">
          <MemoryRouter>
            <RunDetail runId={runId} />
          </MemoryRouter>
        </MantineProvider>
      </QueryClientProvider>
    );
  });
}

async function unmount(): Promise<void> {
  await act(async () => root?.unmount());
  root = null;
  queryClient?.clear();
  queryClient = null;
}

async function clickLogs(): Promise<void> {
  const button = Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === "Logs"
  );
  if (!button) throw new Error("Logs tab was not rendered");
  await act(async () => button.click());
}

async function flushQueryNotifications(): Promise<void> {
  await act(async () => new Promise<void>((resolve) => setTimeout(resolve, 0)));
}

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(async () => {
  if (root) await unmount();
  container?.remove();
});

it("keeps run metadata and LLM requests scoped to the selected run after older requests settle", async () => {
  const oldRun = deferred<RunResponse>();
  const oldLLM = deferred<LLMResponse>();
  vi.mocked(fetchRun).mockImplementation((runId) => (runId === "run-A" ? oldRun.promise : Promise.resolve(run(runId))));
  vi.mocked(fetchLLMRequests).mockImplementation((runId) =>
    runId === "run-A" ? oldLLM.promise : Promise.resolve(llmResponse("model-B"))
  );

  await mount("run-A");
  expect(fetchRun).toHaveBeenCalledWith("run-A", expect.any(AbortSignal));
  expect(fetchLLMRequests).toHaveBeenCalledWith("run-A", expect.any(AbortSignal));

  const oldSignal = vi.mocked(fetchLLMRequests).mock.calls[0]?.[1];
  await switchRun("run-B");
  await flushQueryNotifications();
  expect(oldSignal?.aborted).toBe(true);
  expect(fetchRun).toHaveBeenCalledWith("run-B", expect.any(AbortSignal));
  expect(fetchLLMRequests).toHaveBeenCalledWith("run-B", expect.any(AbortSignal));
  expect(container.textContent).toContain("run-B");
  expect(container.textContent).toContain("model-B");

  await act(async () => {
    oldRun.reject(new Error("old run request failed"));
    oldLLM.resolve(llmResponse("model-A"));
    await Promise.resolve();
  });

  expect(container.textContent).toContain("run-B");
  expect(container.textContent).toContain("model-B");
  expect(container.textContent).not.toContain("model-A");
  expect(toast.error).not.toHaveBeenCalled();
});

it("hydrates the new snapshot and logs while old snapshot and log requests are still pending", async () => {
  const oldSnapshot = deferred<SnapshotResponse>();
  const oldLogs = deferred<Awaited<ReturnType<typeof fetchRunLogs>>>();
  const newSnapshot = deferred<SnapshotResponse>();
  vi.mocked(fetchRun).mockImplementation(async (runId) => run(runId, `snapshot-${runId}`));
  vi.mocked(fetchLLMRequests).mockResolvedValue({ requests: [] });
  vi.mocked(fetchSnapshotDetail).mockImplementation((slug) =>
    slug === "snapshot-run-A" ? oldSnapshot.promise : newSnapshot.promise
  );
  vi.mocked(fetchSnapshotFiles).mockResolvedValue({ files: [], missing: [] });
  vi.mocked(fetchRunLogs).mockImplementation((runId) =>
    runId === "run-A" ? oldLogs.promise : Promise.resolve({ run_id: runId, logs: "logs-for-run-B" })
  );

  await mount("run-A");
  await flushQueryNotifications();
  expect(fetchSnapshotDetail).toHaveBeenCalledWith(
    "snapshot-run-A",
    "whole_snapshot",
    undefined,
    expect.any(AbortSignal)
  );
  await clickLogs();
  await flushQueryNotifications();
  const oldLogSignal = vi.mocked(fetchRunLogs).mock.calls[0]?.[1];

  await switchRun("run-B");
  await flushQueryNotifications();
  expect(oldLogSignal?.aborted).toBe(true);
  expect(fetchSnapshotDetail).toHaveBeenCalledWith(
    "snapshot-run-B",
    "whole_snapshot",
    undefined,
    expect.any(AbortSignal)
  );

  await act(async () => {
    newSnapshot.resolve(snapshotResponse());
    await Promise.resolve();
  });
  await flushQueryNotifications();
  expect(fetchSnapshotFiles).toHaveBeenCalledWith("snapshot-run-B", ["run-B-source.ts"], expect.any(AbortSignal));

  await act(async () => {
    oldSnapshot.resolve(snapshotResponse());
    await Promise.resolve();
  });
  await flushQueryNotifications();
  expect(fetchSnapshotFiles).toHaveBeenCalledTimes(1);
  expect(vi.mocked(fetchSnapshotFiles).mock.calls[0]?.[0]).toBe("snapshot-run-B");

  await clickLogs();
  await flushQueryNotifications();
  expect(fetchRunLogs).toHaveBeenCalledWith("run-B", expect.any(AbortSignal));
  expect(container.textContent).toContain("logs-for-run-B");

  await act(async () => {
    oldLogs.reject(new Error("old log request failed"));
    await Promise.resolve();
  });
  expect(container.textContent).toContain("run-B");
  expect(container.textContent).toContain("logs-for-run-B");
  expect(toast.error).not.toHaveBeenCalled();
});

it("ignores a lazy request failure after the component unmounts", async () => {
  const pendingLogs = deferred<Awaited<ReturnType<typeof fetchRunLogs>>>();
  vi.mocked(fetchRun).mockResolvedValue(run("run-C"));
  vi.mocked(fetchLLMRequests).mockResolvedValue({ requests: [] });
  vi.mocked(fetchRunLogs).mockReturnValue(pendingLogs.promise);

  await mount("run-C");
  await flushQueryNotifications();
  await clickLogs();
  await flushQueryNotifications();
  const signal = vi.mocked(fetchRunLogs).mock.calls[0]?.[1];

  await unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    pendingLogs.reject(new Error("request completed after unmount"));
    await Promise.resolve();
  });
  expect(toast.error).not.toHaveBeenCalled();
});

it("fetches logs lazily and refreshes run and tab data when revisited", async () => {
  vi.mocked(fetchRun).mockImplementation(async (runId) => run(runId));
  vi.mocked(fetchLLMRequests).mockResolvedValue({ requests: [] });
  vi.mocked(fetchRunLogs).mockImplementation(async (runId) => ({ run_id: runId, logs: `logs-for-${runId}` }));

  await mount("run-cache-A");
  await flushQueryNotifications();
  expect(fetchRunLogs).not.toHaveBeenCalled();
  await clickLogs();
  await flushQueryNotifications();
  expect(container.textContent).toContain("logs-for-run-cache-A");
  expect(fetchRunLogs).toHaveBeenCalledTimes(1);

  await switchRun("run-cache-B");
  await flushQueryNotifications();
  await switchRun("run-cache-A");
  await flushQueryNotifications();
  expect(fetchRun).toHaveBeenCalledTimes(3);
  expect(fetchRunLogs).toHaveBeenCalledTimes(1);

  await clickLogs();
  await flushQueryNotifications();
  expect(fetchRunLogs).toHaveBeenCalledTimes(2);
  await clickLogs();
  await flushQueryNotifications();
  expect(fetchRunLogs).toHaveBeenCalledTimes(3);
  expect(container.textContent).toContain("logs-for-run-cache-A");
});

it("keeps recall snapshot cache entries separate by example kind and files hash", async () => {
  vi.mocked(fetchRun).mockImplementation(async (runId) =>
    run(runId, "shared/snapshot", runId === "run-scope-A" ? "files-A" : "files-B")
  );
  vi.mocked(fetchLLMRequests).mockResolvedValue({ requests: [] });
  vi.mocked(fetchSnapshotDetail).mockResolvedValue(snapshotResponse("shared/snapshot"));
  vi.mocked(fetchSnapshotFiles).mockResolvedValue({ files: [], missing: [] });

  await mount("run-scope-A");
  await flushQueryNotifications();
  expect(fetchSnapshotDetail).toHaveBeenCalledWith("shared/snapshot", "file_set", "files-A", expect.any(AbortSignal));

  await switchRun("run-scope-B");
  await flushQueryNotifications();
  expect(fetchSnapshotDetail).toHaveBeenCalledWith("shared/snapshot", "file_set", "files-B", expect.any(AbortSignal));
  expect(fetchSnapshotDetail).toHaveBeenCalledTimes(2);
});

it("shows the selected run's load failure and reports it", async () => {
  vi.mocked(fetchRun).mockRejectedValue(new Error("run is unavailable"));
  vi.mocked(fetchLLMRequests).mockResolvedValue({ requests: [] });

  await mount("run-D");
  await flushQueryNotifications();

  expect(container.textContent).toContain("run is unavailable");
  expect(toast.error).toHaveBeenCalledWith("run is unavailable");
});
