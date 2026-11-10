// @vitest-environment happy-dom
import { SandboxPhase, OperatingMode, ReadinessState } from "../../../environments/v1/environment_service_pb";
import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import {
  FrontendConfigSchema,
  HarnessModelOffersSchema,
  GetFrontendConfigResponseSchema,
  SandboxPresetSchema,
  ThreadMetadataSchema,
  TraceSearchSettingsSchema,
  type LangfuseSearchSettings,
  type SessionDefaultsSchema,
  type TraceSearchSettings,
} from "../../app_service_pb";
import { FeedStatus, SessionSummarySchema } from "../../../history_service/history_service_pb";

import { readySandbox as makeReadySandbox } from "../testing/sandbox";
import { MantineProvider } from "@mantine/core";
import type { ThreadView } from "../threads/client";
import {
  TEST_REASONING_EFFORTS,
  testModelCatalog,
  testModelCatalogProtoJSON,
  testSandboxPresetsProtoJSON,
} from "../testing/model_catalog";
import { serveEventStreams } from "../testing/watch";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { Link, MemoryRouter, Route, Routes } from "react-router";
import { afterAll, afterEach, expect, it, vi, type Mock } from "vitest";

import * as sandboxClient from "./client";
import { LANGFUSE_SEARCH_SETTINGS } from "../testing/langfuse_search";
import { acknowledgedQuiescingLifecycleFixture, runnerStatusFixture } from "../testing/runner_status";
import type { Live } from "../live";
import { SandboxRoute } from "../app";
import { SandboxPage } from "./sandbox_page";
import { TopbarContext } from "../topbar";
import type * as SandboxLiveModule from "./live";
import {
  EnvironmentRuntimeLifecycleMode,
  EnvironmentRuntimeLifecycleStage,
  Harness,
  HarnessState,
  ProcessState,
  SetupState,
  SessionExecutionState,
} from "../../../proto/session_pb";
import { threadView } from "../threads/testing/state";

const fetchMock = vi.hoisted(() => {
  const fetch = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
});

const live = vi.hoisted(
  () =>
    ({
      snapshot: {
        sandbox_uid: "00000000-0000-4000-8000-000000000001" as string | null,
        thread_snapshot_uid: null as string | null,
        threads: [] as ThreadView[] | { kind: "unavailable"; code: string },
        bindings: [],
        updates_connected: true as boolean,
        watch: { healthy: true, failing: {} },
      },
      health: { healthy: true, failing: {} },
      stream: {
        name: "Sandbox startup-test",
        connection: { phase: "live", since: 0 },
        stalled: null,
        standing: "current",
      },
    }) satisfies Live<{
      sandbox_uid: string | null;
      bindings: never[];
      watch: { healthy: boolean; failing: Record<string, never> };
      thread_snapshot_uid: string | null;
      threads: ThreadView[] | { kind: "unavailable"; code: string };
      updates_connected: boolean;
    }>
);
vi.mock("../policy/watch", () => ({
  usePolicyResources: () => ({ resources: { bindings: [], sets: [] }, error: null }),
  useIdentities: () => ({ identities: [], error: null }),
}));

vi.mock("../threads/live", () => ({
  useThreadsLive: (listing: { sandboxUID: string } | null) => ({
    snapshot: Array.isArray(live.snapshot.threads)
      ? {
          threads: live.snapshot.threads,
          sandbox_uid: live.snapshot.thread_snapshot_uid ?? listing?.sandboxUID,
          more: false,
          updates_connected: live.snapshot.updates_connected,
        }
      : null,
    stream: {
      stalled: Array.isArray(live.snapshot.threads) ? null : live.snapshot.threads.code,
      standing: "current",
    },
    health: live.health,
  }),
}));

const readySandbox = makeReadySandbox("startup-test", "00000000-0000-4000-8000-000000000001");
readySandbox.configuration!.preset = "test-preset";
const inventory = {
  snapshot: [readySandbox],
  health: { healthy: true, failing: {} },
  stream: { standing: "current", connection: { phase: "live", since: 0 } },
};
vi.mock("./live", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxLiveModule>()),
  useRequiredSandboxesLive: () => inventory,
  useEnvironmentWatch: (target: { uid: string } | undefined) => ({
    environment: inventory.snapshot.find((item) => item.target?.uid === target?.uid) ?? null,
    stream: inventory.stream,
  }),
}));
const mutableSnapshot = live.snapshot;
const readyPod = readySandbox.kubernetesDiagnostics!.pod;
const readyStatus = readySandbox.kubernetesDiagnostics!.sandboxStatus;

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
let rerender: () => Promise<void> = async () => {};
type SessionDefaultsInit = MessageInitShape<typeof SessionDefaultsSchema>;
const DEFAULT_PRESET_DEFAULTS: SessionDefaultsInit = {
  harness: Harness.CLAUDE,
  model: "test-model",
  reasoningEffort: "low",
};
let centralSamplerEnabled = false;
let presetDefaults: SessionDefaultsInit = { ...DEFAULT_PRESET_DEFAULTS };
let modelCatalogResponse: Promise<Response> | null = null;
const TRACE_SEARCH_SETTINGS: TraceSearchSettings = create(TraceSearchSettingsSchema, {
  grafanaBaseUrl: "https://grafana.test.invalid",
  tempoDatasourceUid: "tempo-test",
  deploymentEnvironment: "agentplane-test",
});
let traceSearchSettings: TraceSearchSettings | null = TRACE_SEARCH_SETTINGS;
let langfuseSearchSettings: LangfuseSearchSettings | null = LANGFUSE_SEARCH_SETTINGS;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  live.snapshot.threads = [];
  live.snapshot.thread_snapshot_uid = null;
  readySandbox.observedState!.operatingMode = OperatingMode.RUNNING;
  readySandbox.observedState!.phase = SandboxPhase.RUNNING;
  readySandbox.kubernetesDiagnostics!.pod = readyPod;
  readySandbox.kubernetesDiagnostics!.sandboxStatus = readyStatus;
  readySandbox.observedState!.readiness!.state = ReadinessState.READY;
  readySandbox.target!.uid = "00000000-0000-4000-8000-000000000001";
  mutableSnapshot.sandbox_uid = readySandbox.target!.uid;
  inventory.snapshot = [readySandbox];
  sessionStorage.clear();
  rerender = async () => {};
  readySandbox.configuration!.binding = undefined;
  presetDefaults = { ...DEFAULT_PRESET_DEFAULTS };
  modelCatalogResponse = null;
  centralSamplerEnabled = false;
  traceSearchSettings = TRACE_SEARCH_SETTINGS;
  langfuseSearchSettings = LANGFUSE_SEARCH_SETTINGS;
});
afterAll(() => vi.unstubAllGlobals());

function requestFromFetchInput(input: RequestInfo | URL, init?: RequestInit): Request {
  if (input instanceof Request) return input;
  return new Request(input instanceof URL ? input : new URL(input, window.location.href), init);
}

async function render(
  sessions: (request: Request) => Promise<Response>,
  threadActions: (request: Request) => Promise<Response> = async () => new Response(null, { status: 204 }),
  claudePaused = false,
  routeNavigation = false,
  runnerStatus: () => Promise<Response> = async () =>
    Response.json(
      runnerStatusFixture({
        environmentRuntimeBootId: "boot-visual-001",
        processes: [
          {
            processId: "process-claude-001",
            pid: 4312n,
            harness: Harness.CLAUDE,
            state: ProcessState.RUNNING,
            assignedSessionIds: ["session-ready-001"],
          },
          {
            processId: "process-codex-002",
            harness: Harness.CODEX,
            state: ProcessState.STARTING,
            assignedSessionIds: ["session-preparing-002"],
          },
        ],
        sessions: [
          {
            sessionId: "session-ready-001",
            processId: "process-claude-001",
            nativeSessionId: "native-claude-001",
            executionState: SessionExecutionState.DORMANT,
            busy: false,
          },
          {
            sessionId: "session-preparing-002",
            processId: "process-codex-002",
            executionState: SessionExecutionState.PREPARING,
            busy: false,
          },
        ],
      })
    )
): Promise<ReturnType<typeof vi.fn>> {
  if (routeNavigation) inventory.snapshot.push(makeReadySandbox("other-test", "00000000-0000-4000-8000-000000000002"));
  // The Egress log panel stays mounted behind its tab and follows a stream that these tests never feed.
  serveEventStreams(() => undefined);
  fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const request = requestFromFetchInput(input, init);
    const path = new URL(request.url).pathname;
    if (path === "/upstream/app-config/v1/models") {
      if (modelCatalogResponse) return modelCatalogResponse;
      const models = [{ model: "test-model", displayName: "Test Model", reasoningEfforts: TEST_REASONING_EFFORTS }];
      const codexModels = routeNavigation
        ? [{ model: "codex-model", displayName: "Codex Model", reasoningEfforts: TEST_REASONING_EFFORTS }]
        : [];
      const catalog = testModelCatalog(claudePaused ? [] : models, claudePaused ? models : codexModels);
      if (centralSamplerEnabled) {
        catalog.harnesses.push(
          create(HarnessModelOffersSchema, { harness: Harness.CENTRAL_SAMPLER, models: ["test-model"] })
        );
      }
      return Promise.resolve(Response.json(testModelCatalogProtoJSON(catalog)));
    }
    if (path === "/upstream/app-config/v1/sandbox-form") {
      return Promise.resolve(Response.json({ policySets: [], newSandboxDefaults: { wakeOnSend: false } }));
    }
    if (path === "/upstream/app-config/v1/frontend-config") {
      return Promise.resolve(
        Response.json(
          toJson(
            GetFrontendConfigResponseSchema,
            create(GetFrontendConfigResponseSchema, {
              frontendConfig: create(FrontendConfigSchema, {
                traceSearch: traceSearchSettings ?? undefined,
                langfuseSearch: langfuseSearchSettings ?? undefined,
              }),
            })
          )
        )
      );
    }
    if (path === "/upstream/app-config/v1/presets") {
      const preset = create(SandboxPresetSchema, {
        name: "test-preset",
        title: "Test preset",
        template: "runner",
        sessionDefaults: { harness: Harness.CLAUDE, ...presetDefaults },
      });
      return Promise.resolve(Response.json(testSandboxPresetsProtoJSON([preset])));
    }
    if (/^\/sandboxes\/(startup-test|other-test)\/runner-status$/.test(path)) return runnerStatus();
    if (/^\/upstream\/app\/v1\/threads\/[^/]+$/.test(path)) {
      return Promise.resolve(Response.json({ detail: "not found" }, { status: 404 }));
    }
    if (/^\/upstream\/history\/v1\/sessions\/[^/]+\/summary$/.test(path)) {
      return Promise.resolve(Response.json({ detail: "not found" }, { status: 404 }));
    }
    if (/^\/upstream\/dispatcher\/v1\/sessions\/[^/]+$/.test(path)) return sessions(request);
    const threadPath = /^\/threads\/([^/]+)$/.exec(path);
    if (threadPath && request.method === "GET") {
      const threads = Array.isArray(live.snapshot.threads) ? live.snapshot.threads : [];
      const found = threads.find((thread) => thread.metadata.threadId === threadPath[1]);
      return Promise.resolve(
        found
          ? Response.json({
              metadata: toJson(ThreadMetadataSchema, found.metadata),
              summary: toJson(SessionSummarySchema, found.summary),
            })
          : Response.json({ detail: "not found" }, { status: 404 })
      );
    }
    if (/^\/sandboxes\/(startup-test|other-test)\/sessions/.test(path)) return sessions(request);
    if (/^\/upstream\/app\/v1\/threads\/[^/]+\/(un)?archive$/.test(path)) return threadActions(request);
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const onOpenThread = vi.fn();
  // The shell's topbar slots, which the page portals its title and actions into.
  const topbarTitle = document.createElement("div");
  const topbarActions = document.createElement("div");
  const app = () => (
    <MantineProvider env="test">
      <TopbarContext.Provider value={{ title: topbarTitle, actions: topbarActions }}>
        <MemoryRouter initialEntries={routeNavigation ? ["/sandboxes/startup-test"] : undefined}>
          {routeNavigation ? (
            <>
              <Link to="/sandboxes/other-test">Other sandbox</Link>
              <Routes>
                <Route path="/sandboxes/:name" element={<SandboxRoute />} />
              </Routes>
            </>
          ) : (
            <SandboxPage name="startup-test" onBack={vi.fn()} onOpenThread={onOpenThread} />
          )}
        </MemoryRouter>
      </TopbarContext.Provider>
    </MantineProvider>
  );
  await act(async () => root.render(app()));
  // After the first render, which clears whatever the container held.
  container.append(topbarTitle, topbarActions);
  rerender = async () => act(async () => root.render(app()));
  return onOpenThread;
}

function tab(label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(
    (node) => node.textContent === label
  );
  if (!found) throw new Error(`Missing ${label} tab`);
  return found;
}

function newThread(): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "New thread");
  if (!button) throw new Error("Missing new thread button");
  return button;
}

function createThreadButton(): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((node) =>
    /Create thread|Creating thread/.test(node.textContent ?? "")
  );
  if (!button) throw new Error("Missing create thread button");
  return button;
}

async function createThreadTooltip(): Promise<Element | null> {
  if (newThread().getAttribute("aria-expanded") !== "true") await act(async () => newThread().click());
  await act(async () => createThreadButton().parentElement!.focus());
  return document.querySelector('[role="tooltip"]');
}

/** Opens the form in place, as an operator does, and creates a Thread from it. */
async function submitNewThread(): Promise<void> {
  if (newThread().getAttribute("aria-expanded") !== "true") await act(async () => newThread().click());
  await act(async () => createThreadButton().click());
}

function labeledInput(label: string): HTMLInputElement {
  const element = [...container.querySelectorAll("label")].find((node) => node.textContent === label);
  const control = element?.control;
  if (!(control instanceof HTMLInputElement)) throw new Error(`Missing ${label} input`);
  return control;
}

async function choose(label: string, value: string): Promise<void> {
  const field = labeledInput(label);
  await act(async () => field.click());
  const list = document.getElementById(field.getAttribute("aria-controls") ?? "");
  const option = [...(list?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])].find(
    (node) => node.textContent === value
  );
  if (!option) throw new Error(`Missing ${value} option`);
  await act(async () => option.click());
}

it("does not join an old detail frame to a same-name replacement", async () => {
  mutableSnapshot.sandbox_uid = "00000000-0000-4000-8000-000000000099";
  live.snapshot.thread_snapshot_uid = mutableSnapshot.sandbox_uid;
  live.snapshot.threads = [threadView({ metadata: { threadId: "old-thread", name: "Old incarnation thread" } })];
  await render(async () => Response.json([]));
  expect(container.textContent).not.toContain("Old incarnation thread");
  expect(container.textContent).not.toContain("There is no sandbox startup-test any more.");
  expect(container.querySelector('[aria-label="Sandbox status: Ready"]')).not.toBeNull();
  if (newThread().getAttribute("aria-expanded") !== "true") await act(async () => newThread().click());
  expect(createThreadButton().disabled).toBe(false);
});

it("does not use an incomplete target as an environment identity", async () => {
  const namespace = readySandbox.target!.namespace;
  readySandbox.target!.namespace = "";
  live.snapshot.threads = [threadView({ metadata: { threadId: "old-thread", name: "Old incarnation thread" } })];
  try {
    await render(async () => Response.json([]));
    expect(container.textContent).not.toContain("Old incarnation thread");
    expect(createThreadButton().disabled).toBe(true);
  } finally {
    readySandbox.target!.namespace = namespace;
  }
});

it("resets the creation form when navigating directly between sandbox routes", async () => {
  await render(async () => Response.json([]), undefined, false, true);
  await act(async () => newThread().click());
  await choose("Harness", "Codex");
  expect(labeledInput("Model").value).toBe("Codex Model");
  const instructions = container.querySelector('textarea[aria-label="Standing instructions"]');
  if (!(instructions instanceof HTMLTextAreaElement)) throw new Error("Missing instructions field");
  const setTextareaValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  if (!setTextareaValue) throw new Error("Missing native textarea value setter");
  await act(async () => {
    setTextareaValue.call(instructions, "Only for startup-test");
    instructions.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await choose("Reasoning effort", "medium");
  expect(instructions.value).toBe("Only for startup-test");

  await act(async () => container.querySelector<HTMLAnchorElement>('a[href="/sandboxes/other-test"]')!.click());

  expect(labeledInput("Harness").value).toBe("Claude");
  expect(labeledInput("Model").value).toBe("Test Model");
  expect(labeledInput("Reasoning effort").value).toBe("low");
  const nextInstructions = container.querySelector('textarea[aria-label="Standing instructions"]');
  expect(nextInstructions).toBeInstanceOf(HTMLTextAreaElement);
  expect((nextInstructions as HTMLTextAreaElement).value).toBe("");
});

it("shows runner process and Session diagnostics, including a dormant Session", async () => {
  await render(async () => Response.json([]));
  await act(async () => tab("Status").click());
  await vi.waitFor(() => expect(container.textContent).toContain("boot-visual-001"));
  expect(container.textContent).toContain("Snapshot read");
  expect(container.textContent).toContain("Harness processes");
  expect(container.textContent).toContain("PID 4312");
  expect(container.textContent).toContain("process-claude-001");
  expect(container.textContent).toContain("Codex");
  expect(container.textContent).toContain("Starting");
  expect(container.textContent).toContain("PID not started");
  expect(container.textContent).toContain("session-ready-001");
  expect(container.textContent).toContain("session-preparing-002");
  expect(container.textContent).toContain("native-claude-001");
  expect(container.textContent).toContain("Dormant");
});

it("saves the wake policy from the Status tab", async () => {
  await render(async () => Response.json([]));
  await act(async () => tab("Status").click());
  const patch = vi.spyOn(sandboxClient, "setEnvironmentWakeOnSend").mockResolvedValue(undefined);
  try {
    const wake = container.querySelector<HTMLInputElement>('input[aria-label="Wake on message for startup-test"]');
    if (!wake) throw new Error("Missing Wake on message checkbox");
    await act(async () => wake.click());

    expect(patch).toHaveBeenCalledWith(readySandbox.target, true);
  } finally {
    patch.mockRestore();
  }
});

/** The Status tab's Kubernetes item with this title, which is open when its button says so. */
function kubernetesItem(title: string): HTMLButtonElement {
  const found = [...container.querySelectorAll<HTMLButtonElement>("button[aria-expanded]")].find((node) =>
    node.textContent?.startsWith(title)
  );
  if (!found) throw new Error(`Missing ${title} item`);
  return found;
}

it("opens the Kubernetes status items that have an unmet condition and no others", async () => {
  readySandbox.kubernetesDiagnostics!.sandboxStatus = {
    conditions: [{ type: "Ready", status: "False", reason: "TestNotReady" }],
  };
  readySandbox.observedState!.readiness!.state = ReadinessState.NOT_READY;
  readySandbox.kubernetesDiagnostics!.pod = {
    ...readyPod!,
    status: { phase: "Running", conditions: [{ type: "Ready", status: "True", reason: "TestAllMet" }] },
  };
  await render(async () => Response.json([]));
  await act(async () => tab("Status").click());

  expect(kubernetesItem("Sandbox conditions").getAttribute("aria-expanded")).toBe("true");
  expect(kubernetesItem("Pod conditions").getAttribute("aria-expanded")).toBe("false");
  await act(async () => kubernetesItem("Pod conditions").click());
  expect(kubernetesItem("Pod conditions").getAttribute("aria-expanded")).toBe("true");
});

it("refreshes the runner snapshot and replaces old process and Session details", async () => {
  let status: object = runnerStatusFixture({
    environmentRuntimeBootId: "boot-before-refresh",
    lifecycle: acknowledgedQuiescingLifecycleFixture({
      generation: 7n,
      sessionId: "session-before-refresh",
      journalSealUid: "incarnation-before-refresh",
      journalSealCursor: 12n,
      acknowledgementCursor: 11n,
    }),
    processes: [
      {
        processId: "process-before-refresh",
        pid: 4312n,
        harness: Harness.CLAUDE,
        state: ProcessState.RUNNING,
        assignedSessionIds: ["session-before-refresh"],
      },
    ],
    sessions: [
      {
        sessionId: "session-before-refresh",
        processId: "process-before-refresh",
        nativeSessionId: "native-before-refresh",
        executionState: SessionExecutionState.READY,
        busy: false,
      },
    ],
  });
  await render(
    async () => Response.json([]),
    undefined,
    false,
    false,
    async () => Response.json(status)
  );
  await act(async () => tab("Status").click());
  await vi.waitFor(() => expect(container.textContent).toContain("session-before-refresh"));
  expect(container.textContent).toContain("Desired generation 7 · Progress · Flushing history");
  expect(container.textContent).toContain("Blocked at History acknowledgement");
  expect(container.textContent).toContain("Acknowledged cursor · 11 of 12 · sealed yes");

  status = runnerStatusFixture({
    environmentRuntimeBootId: "boot-after-refresh",
    lifecycle: {
      desiredMode: EnvironmentRuntimeLifecycleMode.CLOSING,
      desiredGeneration: 8n,
      progressGeneration: 8n,
      progressAvailable: true,
      progressCurrent: true,
      stage: EnvironmentRuntimeLifecycleStage.CLOSED,
      error: "",
    },
    processes: [
      {
        processId: "process-after-refresh",
        harness: Harness.CODEX,
        state: ProcessState.STARTING,
        assignedSessionIds: ["session-after-refresh"],
      },
    ],
    sessions: [
      {
        sessionId: "session-after-refresh",
        processId: "process-after-refresh",
        executionState: SessionExecutionState.PREPARING,
        busy: false,
      },
    ],
  });
  const refresh = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (button) => button.textContent?.trim() === "Refresh"
  );
  if (!refresh) throw new Error("missing Refresh button");
  await act(async () => refresh.click());
  await vi.waitFor(() => expect(container.textContent).toContain("boot-after-refresh"));
  expect(container.textContent).toContain("Progress · Closed");
  expect(container.textContent).not.toContain("Blocked at History acknowledgement");
  expect(container.textContent).toContain("process-after-refresh");
  expect(container.textContent).toContain("session-after-refresh");
  expect(container.textContent).toContain("PID not started");
  expect(container.textContent).not.toContain("session-before-refresh");
  expect(container.textContent).not.toContain("process-before-refresh");
});

it("reports suspended Runtime diagnostics without a read or wake request", async () => {
  readySandbox.observedState!.operatingMode = OperatingMode.SUSPENDED;
  readySandbox.observedState!.phase = SandboxPhase.SUSPENDED;
  readySandbox.kubernetesDiagnostics!.pod = undefined;
  await render(async () => Response.json([]));
  await act(async () => tab("Status").click());
  await vi.waitFor(() =>
    expect(container.textContent).toContain("Runtime diagnostics are unavailable until it is running")
  );
  const paths = fetchMock.mock.calls.map(([input, init]) => {
    const request = requestFromFetchInput(input, init);
    return [request.method, new URL(request.url).pathname];
  });
  expect(paths).not.toContainEqual(["GET", "/sandboxes/startup-test/runner-status"]);
  expect(paths).not.toContainEqual(["POST", "/sandboxes/startup-test/resume"]);
});

it("reports backend Runtime diagnostics failures as unavailable", async () => {
  await render(
    async () => Response.json([]),
    undefined,
    false,
    false,
    async () => Response.json({ detail: "runner connection unavailable" }, { status: 503 })
  );
  await act(async () => tab("Status").click());
  await vi.waitFor(() => expect(container.textContent).toContain("Runtime diagnostics unavailable"));
  expect(container.textContent).toContain("runner connection unavailable");
});

/** The JSON body of the first POST the page made to the sessions endpoint. */
async function postedBody(sessions: Mock<(request: Request) => Promise<Response>>) {
  const posted = sessions.mock.calls.find(([request]) => request.method === "POST")?.[0];
  if (posted === undefined) throw new Error("no POST reached the sessions endpoint");
  return await posted.clone().json();
}

it("preserves a Sandbox reasoning default through model loading and submits it on later Thread launch", async () => {
  presetDefaults = { harness: Harness.CLAUDE, model: "test-model", reasoningEffort: "high" };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  await render(sessions);
  expect(labeledInput("Reasoning effort").value).toBe("high");
  await submitNewThread();
  expect((await postedBody(sessions)).spec.reasoningEffort).toBe("high");
});

it("uses the configured preset working directory template and setup script for later Threads", async () => {
  presetDefaults = {
    harness: Harness.CLAUDE,
    model: "test-model",
    cwd: "/state/custom/{session_id}/work",
    setupScript: "printf 'ready\\n'",
  };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  await render(sessions);
  await submitNewThread();
  const body = await postedBody(sessions);
  expect(body.spec.cwd).toBe("/state/custom/{session_id}/work");
  expect(body.session_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(body.sandbox_uid).toBe("00000000-0000-4000-8000-000000000001");
  expect(body.setup_script).toBe("printf 'ready\\n'");
});

it("notes a Thread's setup only until it completes", async () => {
  live.snapshot.threads = [
    threadView({
      metadata: { threadId: "legacy" },
      summary: { attached: { harnessState: HarnessState.STOPPED, setupState: undefined } },
    }),
    threadView({
      metadata: { threadId: "prepared" },
      summary: { attached: { harnessState: HarnessState.RUNNING, setupState: SetupState.SUCCEEDED } },
    }),
    threadView({
      metadata: { threadId: "preparing" },
      summary: { attached: { harnessState: HarnessState.STOPPED, setupState: SetupState.RUNNING } },
    }),
    threadView({
      metadata: { threadId: "broken" },
      summary: { attached: { harnessState: HarnessState.STOPPED, setupState: SetupState.FAILED } },
    }),
  ];
  const sessions = vi.fn<(request: Request) => Promise<Response>>(() =>
    Promise.resolve(Response.json({ detail: "History owns the Sandbox inventory" }, { status: 500 }))
  );
  await render(sessions);
  const row = (id: string) =>
    [...container.querySelectorAll("tbody tr")].find((candidate) => candidate.textContent?.includes(id));
  await vi.waitFor(() => expect(row("broken")).toBeDefined());
  expect(row("legacy")?.textContent).not.toContain("Setup");
  expect(row("prepared")?.textContent).not.toContain("Setup");
  expect(row("preparing")?.textContent).toContain("Setup script running");
  expect(row("broken")?.textContent).toContain("Setup script failed");
  expect(sessions).not.toHaveBeenCalled();
});

it("lets a later Thread override the preset reasoning default locally", async () => {
  presetDefaults = { harness: Harness.CLAUDE, model: "test-model", reasoningEffort: "high" };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  await render(sessions);
  await choose("Reasoning effort", "medium");
  await submitNewThread();
  expect((await postedBody(sessions)).spec.reasoningEffort).toBe("medium");
  expect((await postedBody(sessions)).spec.reasoningEffort).toBe("medium");
});

/** The status a Session row's indicator states. */
function sessionStatus(name: string): string | null | undefined {
  const row = [...container.querySelectorAll("tbody tr")].find((candidate) => candidate.textContent?.includes(name));
  return row?.querySelector('[role="img"]')?.getAttribute("aria-label");
}

/** Mantine portals a Menu's dropdown onto `document.body`, so its items live outside `container`. */
function menuItem(text: string): HTMLElement {
  const item = [...document.querySelectorAll('[role="menuitem"]')].find((node) => node.textContent === text);
  if (!(item instanceof HTMLElement)) throw new Error(`Missing menu item ${text}`);
  return item;
}

async function openSandboxActionsMenuForTraceSearch(): Promise<HTMLButtonElement> {
  live.snapshot.threads = [threadView({ metadata: { threadId: "existing-session" } })];
  await render(async () => Response.json([]));
  await act(async () => Promise.resolve());

  const button = container.querySelector<HTMLButtonElement>('[aria-label="More Sandbox actions"]');
  if (!button) throw new Error("Missing Sandbox actions menu");
  await act(async () => button.click());
  return button;
}

async function openThreadActionsMenu(threadId: string): Promise<void> {
  const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (node) => node.getAttribute("aria-label") === `More actions for ${threadId}`
  );
  if (!button) throw new Error(`Missing Thread actions menu for ${threadId}`);
  await act(async () => button.click());
}

it("keeps retained History Threads visible through ready to suspended with no runner inventory read", async () => {
  const retained = threadView({
    metadata: { threadId: "retained-session", name: "Previously active thread" },
    summary: {
      feedStatus: FeedStatus.ACTIVE,
      foldedThrough: 12n,
      attached: {
        harnessState: HarnessState.RUNNING,
        activeTurnId: "turn-last-seen",
        setupState: SetupState.SUCCEEDED,
        lastCursor: 12n,
      },
    },
  });
  live.snapshot.threads = [retained];
  const sessions = vi.fn<(request: Request) => Promise<Response>>(() =>
    Promise.resolve(Response.json({ detail: "runner inventory must not be requested" }, { status: 503 }))
  );
  await render(sessions);
  expect(container.textContent).toContain("Previously active thread");
  expect(createThreadButton().disabled).toBe(false);
  expect(sessionStatus("Previously active thread")).toBe("Turn running · Runner feed active · harness running");

  readySandbox.observedState!.operatingMode = OperatingMode.SUSPENDED;
  readySandbox.observedState!.phase = SandboxPhase.SUSPENDED;
  readySandbox.kubernetesDiagnostics!.pod = undefined;
  await rerender();

  const retainedRow = () =>
    [...container.querySelectorAll("tbody tr")].find((candidate) =>
      candidate.textContent?.includes("Previously active thread")
    );
  const actionButton = () =>
    [...container.querySelectorAll("button")].find(
      (node) => node.getAttribute("aria-label") === "More actions for retained-session"
    );
  expect(retainedRow()).toBeDefined();
  expect(sessionStatus("Previously active thread")).toBe("Sandbox suspended");
  expect(container.textContent).toContain("New threads need a ready Sandbox");
  expect(createThreadButton().disabled).toBe(true);
  expect((await createThreadTooltip())?.textContent).toContain("Resume it or enable wake on send");
  expect(sessions).not.toHaveBeenCalled();

  readySandbox.configuration!.wakeOnSend = true;
  await rerender();
  expect(container.textContent).toContain("Creating a thread will wake the Sandbox.");
  expect(createThreadButton().disabled).toBe(false);
  expect(sessions).not.toHaveBeenCalled();

  readySandbox.configuration!.wakeOnSend = false;
  await rerender();
  expect(createThreadButton().disabled).toBe(true);

  const menuButton = actionButton();
  if (!menuButton) throw new Error("Missing retained Thread actions menu");
  await act(async () => menuButton.click());
  expect((menuItem("Wait for the active turn to finish before archiving") as HTMLButtonElement).disabled).toBe(true);
  await act(async () => menuButton.click());

  // History outlives the Sandbox row itself. Removing the Sandbox must not hide authored Threads.
  mutableSnapshot.sandbox_uid = null;
  inventory.snapshot = [];
  await rerender();
  expect(retainedRow()).toBeDefined();

  live.snapshot.threads = [
    {
      ...retained,
      summary: {
        ...retained.summary,
        attached: { ...retained.summary.attached!, harnessState: HarnessState.STOPPED, activeTurnId: "" },
      },
    },
  ];
  await rerender();
  const stoppedMenuButton = actionButton();
  if (!stoppedMenuButton) throw new Error("Missing retained Thread actions menu after Sandbox deletion");
  await act(async () => stoppedMenuButton.click());
  await act(async () => menuItem("Archive").click());
  live.snapshot.threads = [
    {
      ...retained,
      metadata: { ...retained.metadata, archived: true },
      summary: { ...retained.summary, attached: { ...retained.summary.attached!, harnessState: HarnessState.STOPPED } },
    },
  ];
  await rerender();
  expect(retainedRow()).toBeUndefined();
  await act(async () => labeledInput("Show archived").click());
  expect(retainedRow()).toBeDefined();
});

it("shows creation progress, prevents duplicate clicks, and does not retry failed Creates", async () => {
  let fail!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    fail = resolve;
  });
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    request.method === "GET" ? Promise.resolve(Response.json([])) : pending
  );
  const onOpenThread = await render(sessions);
  await submitNewThread();
  expect(createThreadButton().textContent).toContain("Creating thread");
  expect(createThreadButton().disabled).toBe(true);
  expect((await createThreadTooltip())?.textContent).toContain("A thread is being created");
  await act(async () => createThreadButton().click());
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  await act(async () => fail(Response.json({ detail: "session refused" }, { status: 422 })));
  expect(createThreadButton().disabled).toBe(true);
  expect((await createThreadTooltip())?.textContent).toContain("Check its status");
  expect(container.textContent).toContain("session refused");
  expect(container.textContent).toContain("Check this attempt before creating another thread.");
  expect(onOpenThread).not.toHaveBeenCalled();
});

it("explains model catalog loading and an empty catalog on the disabled create button", async () => {
  presetDefaults = {};
  let deliverCatalog!: (response: Response) => void;
  modelCatalogResponse = new Promise((resolve) => {
    deliverCatalog = resolve;
  });
  await render(async () => Response.json([]));
  await act(async () => newThread().click());
  expect(createThreadButton().disabled).toBe(true);
  expect((await createThreadTooltip())?.textContent).toContain("Waiting for the model catalog");
  await act(async () => deliverCatalog(Response.json(testModelCatalogProtoJSON(testModelCatalog([], [])))));
  expect(createThreadButton().disabled).toBe(true);
  expect((await createThreadTooltip())?.textContent).toContain("No models are available");
});

function button(label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((node) => node.textContent?.includes(label));
  if (!found) throw new Error(`Missing button ${label}`);
  return found;
}

function lostCreate(): Mock<(request: Request) => Promise<Response>> {
  return vi.fn((request: Request) =>
    Promise.resolve(
      request.method === "GET"
        ? Response.json({ detail: "no Session" }, { status: 404 })
        : Response.json({ detail: "connection lost" }, { status: 503 })
    )
  );
}

it("does not retry a Create the placement owner has not reported", async () => {
  const sessions = lostCreate();
  const onOpenThread = await render(sessions);
  await submitNewThread();
  const first = await postedBody(sessions);
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBe(first.session_id);
  await act(async () => button("Check Session status").click());
  expect(onOpenThread).not.toHaveBeenCalled();
  expect(container.textContent).toContain("No runner Thread or Session is visible yet");
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  const paths = fetchMock.mock.calls.map(([input, init]) => new URL(requestFromFetchInput(input, init).url).pathname);
  expect(paths).toContain(`/upstream/app/v1/threads/${first.session_id}`);
  expect(paths).toContain(`/upstream/history/v1/sessions/${first.session_id}/summary`);
  expect(paths).toContain(`/upstream/dispatcher/v1/sessions/${first.session_id}`);
});

it("recovers a lost Create from its Thread without sending a second Create", async () => {
  const sessions = lostCreate();
  const onOpenThread = await render(sessions);
  await submitNewThread();
  const first = await postedBody(sessions);
  live.snapshot.threads = [threadView({ metadata: { threadId: first.session_id } })];
  await act(async () => button("Check Session status").click());
  expect(onOpenThread).toHaveBeenCalledWith(first.session_id);
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBeNull();
});

it("resolves an accepted Session from the live matching Thread projection exactly once", async () => {
  const sessions = vi.fn<(request: Request) => Promise<Response>>(async (request) => {
    if (request.method === "GET") return Response.json([]);
    const { session_id: sessionId } = await request.clone().json();
    return Response.json({
      sessionId,
      placement: {
        environmentHosted: {
          environment: { name: "startup-test", uid: readySandbox.target!.uid },
          harness: "HARNESS_CODEX",
        },
      },
      status: { materialization: "SESSION_MATERIALIZATION_WAITING", reason: "waiting for owner" },
    });
  });
  const onOpenThread = await render(sessions);
  await submitNewThread();
  const sessionId = (await postedBody(sessions)).session_id as string;
  expect(container.textContent).toContain("Waiting for the placement owner");
  expect(onOpenThread).not.toHaveBeenCalled();

  live.snapshot.threads = [threadView({ metadata: { threadId: sessionId } })];
  await rerender();
  expect(onOpenThread).toHaveBeenCalledTimes(1);
  expect(onOpenThread).toHaveBeenCalledWith(sessionId);
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBeNull();
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);

  await rerender();
  expect(onOpenThread).toHaveBeenCalledTimes(1);
});

it("resumes a persisted pending Session when its matching Thread is already projected after remount", async () => {
  const sessionId = "recovered-open-session";
  sessionStorage.setItem("agentplane:pending-create:startup-test", sessionId);
  live.snapshot.threads = [threadView({ metadata: { threadId: sessionId } })];
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(
      request.method === "GET" ? Response.json([]) : Response.json({ detail: "unexpected POST" }, { status: 500 })
    )
  );

  const onOpenThread = await render(sessions);
  await vi.waitFor(() => expect(onOpenThread).toHaveBeenCalledWith(sessionId));
  expect(onOpenThread).toHaveBeenCalledTimes(1);
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBeNull();
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(0);
});

it("does not resolve a pending Session from an unrelated Thread or clear it on manual navigation", async () => {
  const sessionId = "pending-session";
  const otherSessionId = "other-session";
  sessionStorage.setItem("agentplane:pending-create:startup-test", sessionId);
  live.snapshot.threads = [threadView({ metadata: { threadId: otherSessionId } })];
  const onOpenThread = await render(async (request) =>
    Response.json(request.method === "GET" ? [{ sessionId: otherSessionId }] : { detail: "unexpected POST" })
  );

  expect(onOpenThread).not.toHaveBeenCalled();
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBe(sessionId);
  await act(async () => {
    const row = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent === otherSessionId);
    if (!(row instanceof HTMLButtonElement)) throw new Error("Missing unrelated Session row");
    row.click();
  });
  expect(onOpenThread).toHaveBeenCalledWith(otherSessionId);
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBe(sessionId);
});

it("reload retains only the Session ID, never the Create spec or bootstrap", async () => {
  const sessions = lostCreate();
  await render(sessions);
  await submitNewThread();
  const original = await postedBody(sessions);
  expect(original.spec).toBeDefined();
  expect(sessionStorage.length).toBe(1);
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBe(original.session_id);
  await act(async () => root.unmount());
  container.remove();
  await render(sessions);
  expect(container.textContent).toContain(original.session_id);
  expect(createThreadButton().disabled).toBe(true);
  expect(button("Forget attempt; Session creation may still complete")).toBeTruthy();
  await act(async () => button("Check Session status").click());
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  await act(async () => button("Forget attempt").click());
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBeNull();
  expect(createThreadButton().disabled).toBe(false);
});

it("opens the client-minted Session ID returned by CreateSession", async () => {
  let publicId = "";
  const sessions = vi.fn<(request: Request) => Promise<Response>>(async (request) => {
    if (request.method === "GET") return Response.json([]);
    publicId = (await request.clone().json()).session_id;
    live.snapshot.threads = [threadView({ metadata: { threadId: publicId } })];
    return Response.json({
      sessionId: publicId,
      placement: {
        environmentHosted: {
          environment: { name: "startup-test", uid: readySandbox.target!.uid },
          harness: "HARNESS_CODEX",
        },
      },
      status: { materialization: "SESSION_MATERIALIZATION_ACCEPTED" },
    });
  });
  const onOpenThread = await render(sessions);
  await submitNewThread();
  expect(onOpenThread).toHaveBeenCalledWith(publicId);
  expect((await postedBody(sessions)).session_id).toBe(publicId);
  expect(sessionStorage.getItem("agentplane:pending-create:startup-test")).toBeNull();
});

it("hides an archived Thread by default, reveals it via Show archived, and unarchives it", async () => {
  live.snapshot.threads = [threadView({ metadata: { threadId: "existing-session", archived: true } })];
  const archiveRequests: Request[] = [];
  await render(
    async () => Response.json([{ sessionId: "existing-session" }]),
    async (request) => {
      archiveRequests.push(request);
      return new Response(null, { status: 204 });
    }
  );
  expect(container.textContent).not.toContain("existing-session");

  await act(async () => labeledInput("Show archived").click());
  expect(container.textContent).toContain("existing-session");

  const menuButton = [...container.querySelectorAll("button")].find(
    (node) => node.getAttribute("aria-label") === "More actions for existing-session"
  );
  if (!menuButton) throw new Error("Missing per-thread actions menu");
  await act(async () => menuButton.click());
  await act(async () => menuItem("Unarchive").click());

  expect(archiveRequests.map((request) => [request.method, new URL(request.url).pathname])).toEqual([
    ["POST", "/upstream/app/v1/threads/existing-session/unarchive"],
  ]);
});

it("allows archiving an idle Thread while its harness is running", async () => {
  live.snapshot.threads = [
    threadView({
      metadata: { threadId: "existing-session" },
      summary: { attached: { harnessState: HarnessState.RUNNING } },
    }),
  ];
  const archiveRequests: Request[] = [];
  await render(
    async () => Response.json({ detail: "History owns the Sandbox inventory" }, { status: 500 }),
    async (request) => {
      archiveRequests.push(request);
      return new Response(null, { status: 204 });
    }
  );
  const menuButton = [...container.querySelectorAll("button")].find(
    (node) => node.getAttribute("aria-label") === "More actions for existing-session"
  );
  if (!menuButton) throw new Error("Missing per-thread actions menu");
  await act(async () => menuButton.click());
  const archiveItem = menuItem("Archive");
  expect(archiveItem).toBeInstanceOf(HTMLButtonElement);
  expect((archiveItem as HTMLButtonElement).disabled).toBe(false);
  await act(async () => archiveItem.click());
  expect(archiveRequests.map((request) => [request.method, new URL(request.url).pathname])).toEqual([
    ["POST", "/upstream/app/v1/threads/existing-session/archive"],
  ]);
});

it.each([
  [{ activeTurnId: "turn-1" }, "Wait for the active turn to finish before archiving"],
  [{ setupState: SetupState.RUNNING }, "Wait for setup to finish before archiving"],
] as const)("blocks archiving when Thread work is visible: %o", async (overrides, reason) => {
  live.snapshot.threads = [
    threadView({
      metadata: { threadId: "existing-session" },
      summary: { attached: { harnessState: HarnessState.RUNNING, ...overrides } },
    }),
  ];
  await render(async () => Response.json({ detail: "History owns the Sandbox inventory" }, { status: 500 }));
  const menuButton = [...container.querySelectorAll("button")].find(
    (node) => node.getAttribute("aria-label") === "More actions for existing-session"
  );
  if (!menuButton) throw new Error("Missing per-thread actions menu");
  await act(async () => menuButton.click());
  expect((menuItem(reason) as HTMLButtonElement).disabled).toBe(true);
});

it("defaults new threads to an offered harness while retaining existing Claude threads", async () => {
  live.snapshot.threads = [threadView({ metadata: { threadId: "old-session", name: "Existing Claude" } })];
  presetDefaults = { harness: Harness.CODEX, model: "test-model" };
  const sessions = vi.fn<(request: Request) => Promise<Response>>(async (request) =>
    request.method === "GET"
      ? Response.json([
          {
            sessionId: "old-session",
            spec: {
              placement: {
                environmentHosted: {
                  environment: { name: readySandbox.target!.name, uid: readySandbox.target!.uid },
                  harness: "HARNESS_CLAUDE",
                },
              },
            },
          },
        ])
      : Response.json({ detail: "Launch recorded" }, { status: 503 })
  );
  await render(sessions, undefined, true);
  expect(container.textContent).toContain("Existing Claude");
  const harness = labeledInput("Harness");
  expect(harness.value).toBe("Codex");
  await act(async () => harness.click());
  const claude = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (option) => option.textContent === "Claude (no models offered)"
  );
  expect(claude?.hasAttribute("data-combobox-disabled")).toBe(true);
  await act(async () => claude!.click());
  expect(harness.value).toBe("Codex");
  await act(async () => harness.click());
  await submitNewThread();
  const body = await postedBody(sessions);
  expect(body.spec.placement.environmentHosted).toEqual({
    environment: { name: readySandbox.target!.name, uid: readySandbox.target!.uid },
    harness: "HARNESS_CODEX",
  });
});

it("keeps lifecycle controls when History Thread summaries are unavailable", async () => {
  live.snapshot.threads = { kind: "unavailable", code: "UNAUTHENTICATED" };
  await render(async () => Response.json([]));
  expect(container.querySelector('[aria-label="Suspend"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="More Sandbox actions"]')).not.toBeNull();
  expect(container.textContent).toContain("History Thread summaries are unavailable (UNAUTHENTICATED)");
});

it("keeps the creation form closed until asked, and keeps what was entered when it is closed again", async () => {
  await render(async () => Response.json([]));
  expect(newThread().getAttribute("aria-expanded")).toBe("false");
  await act(async () => newThread().click());
  expect(newThread().getAttribute("aria-expanded")).toBe("true");
  await choose("Reasoning effort", "medium");

  await act(async () => newThread().click());
  expect(newThread().getAttribute("aria-expanded")).toBe("false");
  await act(async () => newThread().click());
  expect(labeledInput("Reasoning effort").value).toBe("medium");
});

it("offers Delete in the actions menu only for a suspended Sandbox", async () => {
  await render(async () => Response.json([]));
  const deleteItem = () => {
    const item = [...document.querySelectorAll('[role="menuitem"]')].find((node) =>
      node.textContent?.startsWith("Delete")
    );
    if (!(item instanceof HTMLButtonElement)) throw new Error("Missing Delete menu item");
    return item;
  };
  const menuButton = container.querySelector<HTMLButtonElement>('[aria-label="More Sandbox actions"]');
  if (!menuButton) throw new Error("Missing Sandbox actions menu");
  await act(async () => menuButton.click());
  expect(deleteItem().disabled).toBe(true);
  expect(deleteItem().textContent).toContain("Suspend the sandbox before deleting it");
  await act(async () => menuButton.click());

  readySandbox.observedState!.operatingMode = OperatingMode.SUSPENDED;
  readySandbox.observedState!.phase = SandboxPhase.SUSPENDED;
  readySandbox.kubernetesDiagnostics!.pod = undefined;
  await rerender();
  await act(async () => menuButton.click());
  expect(deleteItem().disabled).toBe(false);
  await act(async () => deleteItem().click());
  expect(document.body.textContent).toContain("Delete startup-test?");
  const deleted = vi.spyOn(sandboxClient, "deleteEnvironment").mockResolvedValue(undefined);
  // The confirmation belongs to the incarnation originally selected, even if its name is reused.
  readySandbox.target!.uid = "00000000-0000-4000-8000-000000000099";
  mutableSnapshot.sandbox_uid = readySandbox.target!.uid;
  await rerender();
  const confirm = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')].find(
    (button) => button.textContent === "Delete"
  );
  if (!confirm) throw new Error("Missing delete confirmation");
  await act(async () => confirm.click());
  expect(deleted).toHaveBeenCalledWith(
    expect.objectContaining({
      name: "startup-test",
      uid: "00000000-0000-4000-8000-000000000001",
    })
  );
});

it("links the Sandbox and its Threads to their scoped Grafana trace searches", async () => {
  const sandboxMenuButton = await openSandboxActionsMenuForTraceSearch();
  const sandboxLink = menuItem("Find Sandbox traces in Grafana");
  expect(sandboxLink).toBeInstanceOf(HTMLAnchorElement);
  expect(new URL((sandboxLink as HTMLAnchorElement).href).searchParams.get("panes")).toContain("span.sandbox.name");
  const langfuseLink = menuItem("Find Sandbox LLM traces in Langfuse");
  expect(langfuseLink).toBeInstanceOf(HTMLAnchorElement);
  expect(new URL((langfuseLink as HTMLAnchorElement).href).searchParams.get("filter")).toBe(
    "metadata;stringObject;attributes.agentplane.namespace;=;agentplane-test," +
      "metadata;stringObject;attributes.agentplane.service_account;=;startup-test"
  );
  await act(async () => sandboxMenuButton.click());

  await openThreadActionsMenu("existing-session");
  const sessionLink = menuItem("Find Thread traces in Grafana");
  expect(sessionLink).toBeInstanceOf(HTMLAnchorElement);
  expect((sessionLink as HTMLAnchorElement).target).toBe("_blank");
  expect(new URL((sessionLink as HTMLAnchorElement).href).searchParams.get("panes")).toContain("span.session.id");
});

it("hides trace links when the app leaves trace search unconfigured", async () => {
  traceSearchSettings = null;
  const sandboxMenuButton = await openSandboxActionsMenuForTraceSearch();
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].some((node) =>
      node.textContent?.includes("Find Sandbox traces in Grafana")
    )
  ).toBe(false);
  await act(async () => sandboxMenuButton.click());

  await openThreadActionsMenu("existing-session");
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].some((node) =>
      node.textContent?.includes("Find Thread traces in Grafana")
    )
  ).toBe(false);
});

it("hides Langfuse links independently when Langfuse search is unconfigured", async () => {
  langfuseSearchSettings = null;
  const sandboxMenuButton = await openSandboxActionsMenuForTraceSearch();
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].some((node) =>
      node.textContent?.includes("Find Sandbox LLM traces in Langfuse")
    )
  ).toBe(false);
  await act(async () => sandboxMenuButton.click());
});

it("does not state a Session's status while Thread updates are not being heard", async () => {
  live.snapshot.threads = [
    threadView({
      metadata: { threadId: "quiet", name: "Quiet thread" },
      summary: { feedStatus: FeedStatus.ACTIVE, attached: { harnessState: HarnessState.RUNNING } },
    }),
  ];
  mutableSnapshot.updates_connected = false;
  try {
    await render(async () => Response.json([]));
    expect(sessionStatus("Quiet thread")).toBe("No live harness confirmed");
  } finally {
    mutableSnapshot.updates_connected = true;
  }
});

it("does not show a previous incarnation's Threads after same-name Sandbox replacement", async () => {
  live.snapshot.thread_snapshot_uid = readySandbox.target!.uid;
  live.snapshot.threads = [threadView({ metadata: { threadId: "old-session", name: "Old Thread" } })];
  await render(async () => Response.json([]));
  expect(container.textContent).toContain("Old Thread");
  await act(async () => {
    readySandbox.target!.uid = "00000000-0000-4000-8000-000000000099";
    await rerender();
  });
  expect(container.textContent).not.toContain("Old Thread");
});

it("launches a configured central sampler while preserving the native offerings", async () => {
  centralSamplerEnabled = true;
  presetDefaults = { harness: Harness.CENTRAL_SAMPLER, model: "test-model", setupScript: "echo executor-setup" };
  const sessions = vi.fn<(request: Request) => Promise<Response>>(async () =>
    Response.json({ detail: "Launch recorded" }, { status: 503 })
  );
  await render(sessions);
  expect(labeledInput("Harness").value).toBe("Central sampler");
  expect((container.querySelector('[aria-label="Thread setup script"]') as HTMLTextAreaElement).value).toBe(
    "echo executor-setup"
  );
  await submitNewThread();
  const body = await postedBody(sessions);
  expect(body.spec.placement.centralSamplerV1).toEqual({
    executorEnvironment: { name: readySandbox.target!.name, uid: readySandbox.target!.uid },
  });
  expect(body.setup_script).toBe("echo executor-setup");
});
