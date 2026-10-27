// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { TEST_REASONING_EFFORTS, testModelCatalog } from "../test_model_catalog";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { Link, MemoryRouter, Route, Routes } from "react-router";
import { afterAll, afterEach, expect, it, vi, type Mock } from "vitest";

import { api } from "../client";
import { type LangfuseSearchSettings } from "../langfuse_search";
import { type TraceSearchSettings } from "../trace_search";
import { type RunnerStatusView, type SandboxView } from "./client";
import { type ThreadView } from "../threads/client";
import { LANGFUSE_SEARCH_SETTINGS } from "../langfuse_search_fixture";
import type * as LiveModule from "../live";
import { type Live } from "../live";
import { type SandboxSnapshot } from "./live";

import { SandboxRoute } from "../app";
import { SandboxPage } from "./sandbox_page";
import { TopbarContext } from "../topbar";

const fetchMock = vi.hoisted(() => {
  const fetch = vi.fn<(request: Request) => Promise<Response>>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
});

const live = vi.hoisted(
  () =>
    ({
      snapshot: {
        sandbox: {
          name: "startup-test",
          uid: "00000000-0000-4000-8000-000000000001",
          namespace: "agentplane-test",
          created_at: "2026-01-01T00:00:00Z",
          operating_mode: "Running",
          wake_on_send: false,
          service_account: { namespace: "agentplane-test", name: "startup-test" },
          preset: "test-preset",
          status: { conditions: [{ type: "Ready", status: "True" }] },
          phase: "Running",
          pod: {
            name: "startup-test",
            namespace: "agentplane-test",
            uid: "test-pod-1",
            deleting: false,
            node_name: "test-node",
            owner_references: [
              {
                api_version: "agentplane.allegedly.works/v1alpha1",
                kind: "Sandbox",
                name: "startup-test",
                uid: "00000000-0000-4000-8000-000000000001",
                controller: true,
              },
            ],
            status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
          },
        },
        threads: [] as SandboxSnapshot["threads"],
        bindings: [],
        action_policy: null,
        updates_connected: true,
        watch: { fresh: true, stale_after_seconds: 60, refreshed_seconds_ago: {} },
      },
      health: { fresh: true, stale_after_seconds: 60, refreshed_seconds_ago: {} },
      stream: {
        name: "Sandbox startup-test",
        connection: { phase: "live", since: 0 },
        stalled: null,
        standing: "current",
      },
    }) satisfies Live<SandboxSnapshot>
);
vi.mock("../live", async (importOriginal) => ({
  ...(await importOriginal<typeof LiveModule>()),
  useLive: () => live,
}));

const readySandbox = live.snapshot.sandbox as SandboxView;
const mutableSnapshot = live.snapshot as SandboxSnapshot;
const readyPod = readySandbox.pod;
const readyStatus = readySandbox.status;

/** The Egress log panel stays mounted behind its tab and follows a stream that these tests never feed. */
class SilentEventSource extends EventTarget {
  static readonly CLOSED = 2;
  readyState = 1;
  constructor(readonly url: string) {
    super();
  }
  close(): void {}
}

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
let rerender: () => Promise<void> = async () => {};
const DEFAULT_PRESET_DEFAULTS = {
  harness: "HARNESS_CLAUDE",
  model: "test-model",
  reasoning_effort: "low",
};
let presetDefaults = { ...DEFAULT_PRESET_DEFAULTS } as Record<string, unknown>;
let modelCatalogResponse: Promise<Response> | null = null;
const TRACE_SEARCH_SETTINGS: TraceSearchSettings = {
  grafana_base_url: "https://grafana.test.invalid",
  tempo_datasource_uid: "tempo-test",
  deployment_environment: "agentplane-test",
};
let traceSearchSettings: TraceSearchSettings | null = TRACE_SEARCH_SETTINGS;
let langfuseSearchSettings: LangfuseSearchSettings | null = LANGFUSE_SEARCH_SETTINGS;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  live.snapshot.threads = [];
  readySandbox.operating_mode = "Running";
  readySandbox.phase = "Running";
  readySandbox.pod = readyPod;
  readySandbox.status = readyStatus;
  mutableSnapshot.sandbox = readySandbox;
  sessionStorage.clear();
  rerender = async () => {};
  (live.snapshot.sandbox as SandboxView).binding = null;
  presetDefaults = { ...DEFAULT_PRESET_DEFAULTS };
  modelCatalogResponse = null;
  traceSearchSettings = TRACE_SEARCH_SETTINGS;
  langfuseSearchSettings = LANGFUSE_SEARCH_SETTINGS;
});
afterAll(() => vi.unstubAllGlobals());

function thread(overrides: Partial<ThreadView> & Pick<ThreadView, "id">): ThreadView {
  return {
    sandbox: "startup-test",
    harness: "HARNESS_CLAUDE",
    model: "test-model",
    cwd: "/work",
    created_at: "2026-01-01T00:00:00Z",
    name: null,
    archived: false,
    last_cursor: 0,
    harness_state: "HARNESS_STATE_UNSPECIFIED",
    ...overrides,
  };
}

async function render(
  sessions: (request: Request) => Promise<Response>,
  threadActions: (request: Request) => Promise<Response> = async () => new Response(null, { status: 204 }),
  claudePaused = false,
  routeNavigation = false,
  runnerStatus: () => Promise<Response> = async () =>
    Response.json({
      runner_boot_id: "boot-visual-001",
      processes: [
        {
          process_id: "process-claude-001",
          pid: 4312,
          harness: "HARNESS_CLAUDE",
          state: "PROCESS_STATE_RUNNING",
          assigned_session_ids: ["session-ready-001"],
        },
        {
          process_id: "process-codex-002",
          pid: null,
          harness: "HARNESS_CODEX",
          state: "PROCESS_STATE_STARTING",
          assigned_session_ids: ["session-preparing-002"],
        },
      ],
      sessions: [
        {
          session_id: "session-ready-001",
          process_id: "process-claude-001",
          native_session_id: "native-claude-001",
          execution_state: "SESSION_EXECUTION_STATE_DORMANT",
          active_turn_id: null,
          failure_cursor: null,
          busy: false,
        },
        {
          session_id: "session-preparing-002",
          process_id: "process-codex-002",
          native_session_id: null,
          execution_state: "SESSION_EXECUTION_STATE_PREPARING",
          active_turn_id: null,
          failure_cursor: null,
          busy: false,
        },
      ],
    })
): Promise<ReturnType<typeof vi.fn>> {
  vi.stubGlobal("EventSource", SilentEventSource);
  fetchMock.mockImplementation((request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/models") {
      if (modelCatalogResponse) return modelCatalogResponse;
      const models = [{ model: "test-model", display_name: "Test Model", reasoning_efforts: TEST_REASONING_EFFORTS }];
      const codexModels = routeNavigation
        ? [{ model: "codex-model", display_name: "Codex Model", reasoning_efforts: TEST_REASONING_EFFORTS }]
        : [];
      return Promise.resolve(
        Response.json(testModelCatalog(claudePaused ? [] : models, claudePaused ? models : codexModels))
      );
    }
    if (path === "/sandboxes/form") {
      return Promise.resolve(Response.json({ policy_sets: [] }));
    }
    if (path === "/frontend-config") {
      return Promise.resolve(
        Response.json({ trace_search: traceSearchSettings, langfuse_search: langfuseSearchSettings })
      );
    }
    if (path === "/presets") {
      return Promise.resolve(
        Response.json([
          {
            name: "test-preset",
            title: "Test preset",
            template: "runner",
            policy_sets: [],
            inbox_tags: [],
            session_defaults: presetDefaults,
            bootstrap: "",
          },
        ])
      );
    }
    if (/^\/sandboxes\/(startup-test|other-test)\/runner-status$/.test(path)) return runnerStatus();
    const threadPath = /^\/threads\/([^/]+)$/.exec(path);
    if (threadPath && request.method === "GET") {
      const threads = Array.isArray(live.snapshot.threads) ? live.snapshot.threads : [];
      const found = threads.find((thread) => thread.id === threadPath[1]);
      return Promise.resolve(found ? Response.json(found) : Response.json({ detail: "not found" }, { status: 404 }));
    }
    if (/^\/sandboxes\/(startup-test|other-test)\/sessions/.test(path)) return sessions(request);
    if (/^\/threads\/[^/]+\/(un)?archive$/.test(path)) return threadActions(request);
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
  const patch = vi
    .spyOn(api, "PATCH")
    .mockResolvedValue({ data: undefined, response: new Response(null, { status: 204 }) } as never);
  try {
    const wake = container.querySelector<HTMLInputElement>('input[aria-label="Wake on message for startup-test"]');
    if (!wake) throw new Error("Missing Wake on message checkbox");
    await act(async () => wake.click());

    expect(patch).toHaveBeenCalledWith("/sandboxes/{name}/wake-policy", {
      params: { path: { name: "startup-test" } },
      body: { uid: readySandbox.uid, wake_on_send: true },
    });
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
  readySandbox.status = { conditions: [{ type: "Ready", status: "False", reason: "TestNotReady" }] };
  readySandbox.pod = {
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
  let status: RunnerStatusView = {
    runner_boot_id: "boot-before-refresh",
    processes: [
      {
        process_id: "process-before-refresh",
        pid: 4312,
        harness: "HARNESS_CLAUDE",
        state: "PROCESS_STATE_RUNNING",
        assigned_session_ids: ["session-before-refresh"],
      },
    ],
    sessions: [
      {
        session_id: "session-before-refresh",
        process_id: "process-before-refresh",
        native_session_id: "native-before-refresh",
        execution_state: "SESSION_EXECUTION_STATE_READY",
        active_turn_id: null,
        failure_cursor: null,
        busy: false,
      },
    ],
  };
  await render(
    async () => Response.json([]),
    undefined,
    false,
    false,
    async () => Response.json(status)
  );
  await act(async () => tab("Status").click());
  await vi.waitFor(() => expect(container.textContent).toContain("session-before-refresh"));

  status = {
    runner_boot_id: "boot-after-refresh",
    processes: [
      {
        process_id: "process-after-refresh",
        pid: null,
        harness: "HARNESS_CODEX",
        state: "PROCESS_STATE_STARTING",
        assigned_session_ids: ["session-after-refresh"],
      },
    ],
    sessions: [
      {
        session_id: "session-after-refresh",
        process_id: "process-after-refresh",
        native_session_id: null,
        execution_state: "SESSION_EXECUTION_STATE_PREPARING",
        active_turn_id: null,
        failure_cursor: null,
        busy: false,
      },
    ],
  };
  const refresh = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (button) => button.textContent?.trim() === "Refresh"
  );
  if (!refresh) throw new Error("missing Refresh button");
  await act(async () => refresh.click());
  await vi.waitFor(() => expect(container.textContent).toContain("boot-after-refresh"));
  expect(container.textContent).toContain("process-after-refresh");
  expect(container.textContent).toContain("session-after-refresh");
  expect(container.textContent).toContain("PID not started");
  expect(container.textContent).not.toContain("session-before-refresh");
  expect(container.textContent).not.toContain("process-before-refresh");
});

it("reports suspended runner diagnostics without a read or wake request", async () => {
  readySandbox.operating_mode = "Suspended";
  readySandbox.phase = "Suspended";
  readySandbox.pod = null;
  await render(async () => Response.json([]));
  await act(async () => tab("Status").click());
  await vi.waitFor(() =>
    expect(container.textContent).toContain("Runner diagnostics are unavailable until it is running")
  );
  const paths = fetchMock.mock.calls.map(([request]) => [request.method, new URL(request.url).pathname]);
  expect(paths).not.toContainEqual(["GET", "/sandboxes/startup-test/runner-status"]);
  expect(paths).not.toContainEqual(["POST", "/sandboxes/startup-test/resume"]);
});

it("reports backend runner diagnostics failures as unavailable", async () => {
  await render(
    async () => Response.json([]),
    undefined,
    false,
    false,
    async () => Response.json({ detail: "runner connection unavailable" }, { status: 503 })
  );
  await act(async () => tab("Status").click());
  await vi.waitFor(() => expect(container.textContent).toContain("Runner diagnostics unavailable"));
  expect(container.textContent).toContain("runner connection unavailable");
});

/** The JSON body of the first POST the page made to the sessions endpoint. */
async function postedBody(sessions: Mock<(request: Request) => Promise<Response>>) {
  const posted = sessions.mock.calls.find(([request]) => request.method === "POST")?.[0];
  if (posted === undefined) throw new Error("no POST reached the sessions endpoint");
  return await posted.clone().json();
}

it("preserves a Sandbox reasoning default through model loading and submits it on later Thread launch", async () => {
  presetDefaults = { harness: "HARNESS_CLAUDE", model: "test-model", reasoning_effort: "high" };
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
    harness: "HARNESS_CLAUDE",
    model: "test-model",
    cwd: "/state/custom/{session_id}/work",
    setup_script: "printf 'ready\\n'",
  };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  await render(sessions);
  await submitNewThread();
  const body = await postedBody(sessions);
  expect(body.spec.cwd).toBe("/state/custom/{session_id}/work");
  expect(body.session_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(body.setup_script).toBe("printf 'ready\\n'");
});

it("notes a Thread's setup only until it completes", async () => {
  live.snapshot.threads = [
    thread({ id: "legacy", harness_state: "HARNESS_STATE_STOPPED", setup_state: null }),
    thread({ id: "prepared", harness_state: "HARNESS_STATE_RUNNING", setup_state: "SETUP_STATE_SUCCEEDED" }),
    thread({ id: "preparing", harness_state: "HARNESS_STATE_STOPPED", setup_state: "SETUP_STATE_RUNNING" }),
    thread({ id: "broken", harness_state: "HARNESS_STATE_STOPPED", setup_state: "SETUP_STATE_FAILED" }),
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
  presetDefaults = { harness: "HARNESS_CLAUDE", model: "test-model", reasoning_effort: "high" };
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

it("keeps retained History Threads visible through ready to suspended with no runner inventory read", async () => {
  const retained = thread({
    id: "retained-session",
    name: "Previously active thread",
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
    active_turn_id: "turn-last-seen",
    setup_state: "SETUP_STATE_SUCCEEDED",
    last_cursor: 12,
  });
  live.snapshot.threads = [retained];
  const sessions = vi.fn<(request: Request) => Promise<Response>>(() =>
    Promise.resolve(Response.json({ detail: "runner inventory must not be requested" }, { status: 503 }))
  );
  await render(sessions);
  expect(container.textContent).toContain("Previously active thread");
  expect(createThreadButton().disabled).toBe(false);
  expect(sessionStatus("Previously active thread")).toBe("Turn running · Runner feed active · harness running");

  readySandbox.operating_mode = "Suspended";
  readySandbox.phase = "Suspended";
  readySandbox.pod = null;
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

  readySandbox.wake_on_send = true;
  await rerender();
  expect(container.textContent).toContain("Creating a thread will wake the Sandbox.");
  expect(createThreadButton().disabled).toBe(false);
  expect(sessions).not.toHaveBeenCalled();

  readySandbox.wake_on_send = false;
  await rerender();
  expect(createThreadButton().disabled).toBe(true);

  const menuButton = actionButton();
  if (!menuButton) throw new Error("Missing retained Thread actions menu");
  await act(async () => menuButton.click());
  expect((menuItem("Wait for the active turn to finish before archiving") as HTMLButtonElement).disabled).toBe(true);
  await act(async () => menuButton.click());

  // History outlives the Sandbox row itself. Removing the Sandbox must not hide authored Threads.
  mutableSnapshot.sandbox = null;
  await rerender();
  expect(retainedRow()).toBeDefined();

  live.snapshot.threads = [{ ...retained, harness_state: "HARNESS_STATE_STOPPED", active_turn_id: null }];
  await rerender();
  const stoppedMenuButton = actionButton();
  if (!stoppedMenuButton) throw new Error("Missing retained Thread actions menu after Sandbox deletion");
  await act(async () => stoppedMenuButton.click());
  await act(async () => menuItem("Archive").click());
  live.snapshot.threads = [{ ...retained, archived: true, harness_state: "HARNESS_STATE_STOPPED" }];
  await rerender();
  expect(retainedRow()).toBeUndefined();
  await act(async () => labeledInput("Show archived").click());
  expect(retainedRow()).toBeDefined();
});

it("shows creation progress, prevents duplicate clicks, and does not retry failed Opens", async () => {
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
  expect((await createThreadTooltip())?.textContent).toContain("Check opening status");
  expect(container.textContent).toContain("session refused");
  expect(container.textContent).toContain("Check opening status");
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
  await act(async () => deliverCatalog(Response.json(testModelCatalog([], []))));
  expect(createThreadButton().disabled).toBe(true);
  expect((await createThreadTooltip())?.textContent).toContain("No models are available");
});

function button(label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((node) => node.textContent?.includes(label));
  if (!found) throw new Error(`Missing button ${label}`);
  return found;
}

function lostOpen(): Mock<(request: Request) => Promise<Response>> {
  return vi.fn((request: Request) =>
    Promise.resolve(
      new URL(request.url).pathname.endsWith("/opening")
        ? Response.json({ detail: "no opening" }, { status: 404 })
        : request.method === "GET"
          ? Response.json([])
          : Response.json({ detail: "connection lost" }, { status: 503 })
    )
  );
}

it("does not retry an Open the runner has not reported", async () => {
  const sessions = lostOpen();
  const onOpenThread = await render(sessions);
  await submitNewThread();
  const first = await postedBody(sessions);
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBe(first.session_id);
  await act(async () => button("Check opening status").click());
  expect(onOpenThread).not.toHaveBeenCalled();
  expect(container.textContent).toContain("No runner Thread or Dispatcher opening is visible yet");
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  expect(fetchMock.mock.calls.map(([request]) => new URL(request.url).pathname)).toContain(
    `/threads/${first.session_id}`
  );
});

it("recovers a lost Open from its Thread without sending a second Open", async () => {
  const sessions = lostOpen();
  const onOpenThread = await render(sessions);
  await submitNewThread();
  const first = await postedBody(sessions);
  live.snapshot.threads = [thread({ id: first.session_id })];
  await act(async () => button("Check opening status").click());
  expect(onOpenThread).toHaveBeenCalledWith(first.session_id);
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBeNull();
});

it("resolves a queued Open from the live matching Thread projection exactly once", async () => {
  const sessions = vi.fn<(request: Request) => Promise<Response>>(async (request) => {
    if (request.method === "GET") return Response.json([]);
    const { session_id: sessionId } = await request.clone().json();
    return Response.json({
      sessionId,
      sandbox: "startup-test",
      state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
      reason: "waiting for runner",
    });
  });
  const onOpenThread = await render(sessions);
  await submitNewThread();
  const sessionId = (await postedBody(sessions)).session_id as string;
  expect(container.textContent).toContain("Queued in Dispatcher");
  expect(onOpenThread).not.toHaveBeenCalled();

  live.snapshot.threads = [thread({ id: sessionId })];
  await rerender();
  expect(onOpenThread).toHaveBeenCalledTimes(1);
  expect(onOpenThread).toHaveBeenCalledWith(sessionId);
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBeNull();
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);

  await rerender();
  expect(onOpenThread).toHaveBeenCalledTimes(1);
});

it("resumes a persisted pending Open when its matching Thread is already projected after remount", async () => {
  const sessionId = "recovered-open-session";
  sessionStorage.setItem("agentplane:pending-open:startup-test", sessionId);
  live.snapshot.threads = [thread({ id: sessionId })];
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(
      request.method === "GET" ? Response.json([]) : Response.json({ detail: "unexpected POST" }, { status: 500 })
    )
  );

  const onOpenThread = await render(sessions);
  await vi.waitFor(() => expect(onOpenThread).toHaveBeenCalledWith(sessionId));
  expect(onOpenThread).toHaveBeenCalledTimes(1);
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBeNull();
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(0);
});

it("does not resolve a pending Open from an unrelated Thread or clear it on manual navigation", async () => {
  const sessionId = "pending-session";
  const otherSessionId = "other-session";
  sessionStorage.setItem("agentplane:pending-open:startup-test", sessionId);
  live.snapshot.threads = [thread({ id: otherSessionId })];
  const onOpenThread = await render(async (request) =>
    Response.json(request.method === "GET" ? [{ sessionId: otherSessionId }] : { detail: "unexpected POST" })
  );

  expect(onOpenThread).not.toHaveBeenCalled();
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBe(sessionId);
  await act(async () => {
    const row = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent === otherSessionId);
    if (!(row instanceof HTMLButtonElement)) throw new Error("Missing unrelated Session row");
    row.click();
  });
  expect(onOpenThread).toHaveBeenCalledWith(otherSessionId);
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBe(sessionId);
});

it("reload retains only the Session ID, never the Open spec or bootstrap", async () => {
  const sessions = lostOpen();
  await render(sessions);
  await submitNewThread();
  const original = await postedBody(sessions);
  expect(original.spec).toBeDefined();
  expect(sessionStorage.length).toBe(1);
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBe(original.session_id);
  await act(async () => root.unmount());
  container.remove();
  await render(sessions);
  expect(container.textContent).toContain(original.session_id);
  expect(createThreadButton().disabled).toBe(true);
  await act(async () => button("Check opening status").click());
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  await act(async () => button("Forget attempt").click());
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBeNull();
  expect(createThreadButton().disabled).toBe(false);
});

it("opens the client-minted Session ID returned by the opening", async () => {
  let publicId = "";
  const sessions = vi.fn<(request: Request) => Promise<Response>>(async (request) => {
    if (request.method === "GET") return Response.json([]);
    publicId = (await request.clone().json()).session_id;
    live.snapshot.threads = [thread({ id: publicId })];
    return Response.json({
      sessionId: publicId,
      sandbox: "startup-test",
      state: "DELIVERY_STATE_RUNNER_ADMITTED",
    });
  });
  const onOpenThread = await render(sessions);
  await submitNewThread();
  expect(onOpenThread).toHaveBeenCalledWith(publicId);
  expect((await postedBody(sessions)).session_id).toBe(publicId);
  expect(sessionStorage.getItem("agentplane:pending-open:startup-test")).toBeNull();
});

it("hides an archived Thread by default, reveals it via Show archived, and unarchives it", async () => {
  live.snapshot.threads = [thread({ id: "existing-session", archived: true })];
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
    ["POST", "/threads/existing-session/unarchive"],
  ]);
});

it("allows archiving an idle Thread while its harness is running", async () => {
  live.snapshot.threads = [thread({ id: "existing-session", harness_state: "HARNESS_STATE_RUNNING" })];
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
    ["POST", "/threads/existing-session/archive"],
  ]);
});

it.each([
  [{ active_turn_id: "turn-1" }, "Wait for the active turn to finish before archiving"],
  [{ setup_state: "SETUP_STATE_RUNNING" }, "Wait for setup to finish before archiving"],
] as const)("blocks archiving when Thread work is visible: %o", async (overrides, reason) => {
  live.snapshot.threads = [thread({ id: "existing-session", harness_state: "HARNESS_STATE_RUNNING", ...overrides })];
  await render(async () => Response.json({ detail: "History owns the Sandbox inventory" }, { status: 500 }));
  const menuButton = [...container.querySelectorAll("button")].find(
    (node) => node.getAttribute("aria-label") === "More actions for existing-session"
  );
  if (!menuButton) throw new Error("Missing per-thread actions menu");
  await act(async () => menuButton.click());
  expect((menuItem(reason) as HTMLButtonElement).disabled).toBe(true);
});

it("defaults new threads to an offered harness while retaining existing Claude threads", async () => {
  live.snapshot.threads = [thread({ id: "old-session", name: "Existing Claude" })];
  presetDefaults = { harness: "HARNESS_CODEX", model: "test-model" };
  const sessions = vi.fn<(request: Request) => Promise<Response>>(async (request) =>
    request.method === "GET"
      ? Response.json([{ sessionId: "old-session", spec: { harness: "HARNESS_CLAUDE" } }])
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
  expect((await postedBody(sessions)).spec.harness).toBe("HARNESS_CODEX");
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

  readySandbox.operating_mode = "Suspended";
  readySandbox.phase = "Suspended";
  readySandbox.pod = null;
  await rerender();
  await act(async () => menuButton.click());
  expect(deleteItem().disabled).toBe(false);
  await act(async () => deleteItem().click());
  expect(document.body.textContent).toContain("Delete startup-test?");
});

it("links the Sandbox and its Threads to their scoped Grafana trace searches", async () => {
  live.snapshot.threads = [thread({ id: "existing-session" })];
  await render(async () => Response.json([]));
  await act(async () => Promise.resolve());

  const sandboxMenuButton = container.querySelector<HTMLButtonElement>('[aria-label="More Sandbox actions"]');
  if (!sandboxMenuButton) throw new Error("Missing Sandbox actions menu");
  await act(async () => sandboxMenuButton.click());
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

  const sessionMenuButton = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (node) => node.getAttribute("aria-label") === "More actions for existing-session"
  );
  if (!sessionMenuButton) throw new Error("Missing Thread actions menu");
  await act(async () => sessionMenuButton.click());
  const sessionLink = menuItem("Find Thread traces in Grafana");
  expect(sessionLink).toBeInstanceOf(HTMLAnchorElement);
  expect((sessionLink as HTMLAnchorElement).target).toBe("_blank");
  expect(new URL((sessionLink as HTMLAnchorElement).href).searchParams.get("panes")).toContain("span.session.id");
});

it("hides trace links when the app leaves trace search unconfigured", async () => {
  traceSearchSettings = null;
  live.snapshot.threads = [thread({ id: "existing-session" })];
  await render(async () => Response.json([]));
  await act(async () => Promise.resolve());

  const sandboxMenuButton = container.querySelector<HTMLButtonElement>('[aria-label="More Sandbox actions"]');
  if (!sandboxMenuButton) throw new Error("Missing Sandbox actions menu");
  await act(async () => sandboxMenuButton.click());
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].some((node) =>
      node.textContent?.includes("Find Sandbox traces in Grafana")
    )
  ).toBe(false);
  await act(async () => sandboxMenuButton.click());

  const sessionMenuButton = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (node) => node.getAttribute("aria-label") === "More actions for existing-session"
  );
  if (!sessionMenuButton) throw new Error("Missing Thread actions menu");
  await act(async () => sessionMenuButton.click());
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].some((node) =>
      node.textContent?.includes("Find Thread traces in Grafana")
    )
  ).toBe(false);
});

it("hides Langfuse links independently when Langfuse search is unconfigured", async () => {
  langfuseSearchSettings = null;
  live.snapshot.threads = [thread({ id: "existing-session" })];
  await render(async () => Response.json([]));
  await act(async () => Promise.resolve());

  const sandboxMenuButton = container.querySelector<HTMLButtonElement>('[aria-label="More Sandbox actions"]');
  if (!sandboxMenuButton) throw new Error("Missing Sandbox actions menu");
  await act(async () => sandboxMenuButton.click());
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].some((node) =>
      node.textContent?.includes("Find Sandbox LLM traces in Langfuse")
    )
  ).toBe(false);
  await act(async () => sandboxMenuButton.click());
});

it("does not state a Session's status while Thread updates are not being heard", async () => {
  live.snapshot.threads = [
    thread({ id: "quiet", name: "Quiet thread", harness_state: "HARNESS_STATE_RUNNING", feed_status: "active" }),
  ];
  mutableSnapshot.updates_connected = false;
  try {
    await render(async () => Response.json([]));
    expect(sessionStatus("Quiet thread")).toBe("No live harness confirmed");
  } finally {
    mutableSnapshot.updates_connected = true;
  }
});
