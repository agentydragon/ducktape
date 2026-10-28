// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { AgentRunDetail, LlmRequestInfo, SnapshotDetailResponse } from "../lib/api/schema";
import { fetchLLMRequests, fetchRun, fetchRunLogs, fetchSnapshotDetail, fetchSnapshotFiles } from "../lib/api/client";
import { toast } from "../lib/toast";
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

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function run(runId: string, snapshotSlug?: string): AgentRunDetail {
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
      ? { agent_type: "critic", example: { kind: "whole_snapshot", snapshot_slug: snapshotSlug } }
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

function snapshotResponse(): SnapshotResponse {
  return {
    slug: "test/snapshot",
    split: "valid",
    created_at: "2026-01-01T00:00:00Z",
    true_positives: [],
    false_positives: [],
  } satisfies SnapshotDetailResponse;
}

let root: Root | null = null;
let container: HTMLDivElement;

async function mount(runId: string): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <RunDetail runId={runId} />
        </MemoryRouter>
      </MantineProvider>
    );
  });
}

async function switchRun(runId: string): Promise<void> {
  await act(async () => {
    root?.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <RunDetail runId={runId} />
        </MemoryRouter>
      </MantineProvider>
    );
  });
}

async function unmount(): Promise<void> {
  await act(async () => root?.unmount());
  root = null;
}

async function clickLogs(): Promise<void> {
  const button = Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === "Logs"
  );
  if (!button) throw new Error("Logs tab was not rendered");
  await act(async () => button.click());
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
  await act(async () => Promise.resolve());
  expect(fetchSnapshotDetail).toHaveBeenCalledWith(
    "snapshot-run-A",
    "whole_snapshot",
    undefined,
    expect.any(AbortSignal)
  );
  await clickLogs();
  await act(async () => Promise.resolve());
  const oldLogSignal = vi.mocked(fetchRunLogs).mock.calls[0]?.[1];

  await switchRun("run-B");
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
  expect(fetchSnapshotFiles).toHaveBeenCalledWith("snapshot-run-B", ["run-B-source.ts"], expect.any(AbortSignal));

  await act(async () => {
    oldSnapshot.resolve(snapshotResponse());
    await Promise.resolve();
  });
  expect(fetchSnapshotFiles).toHaveBeenCalledTimes(1);
  expect(vi.mocked(fetchSnapshotFiles).mock.calls[0]?.[0]).toBe("snapshot-run-B");

  await clickLogs();
  await act(async () => Promise.resolve());
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
  await clickLogs();
  await act(async () => Promise.resolve());
  const signal = vi.mocked(fetchRunLogs).mock.calls[0]?.[1];

  await unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    pendingLogs.reject(new Error("request completed after unmount"));
    await Promise.resolve();
  });
  expect(toast.error).not.toHaveBeenCalled();
});

it("shows the selected run's load failure and reports it", async () => {
  vi.mocked(fetchRun).mockRejectedValue(new Error("run is unavailable"));
  vi.mocked(fetchLLMRequests).mockResolvedValue({ requests: [] });

  await mount("run-D");

  expect(container.textContent).toContain("run is unavailable");
  expect(toast.error).toHaveBeenCalledWith("run is unavailable");
});
