// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { TEST_REASONING_EFFORTS } from "./test_model_catalog";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, afterEach, expect, it, vi } from "vitest";

import type { SandboxView, ThreadView } from "./client";
import type { Live, SandboxSnapshot } from "./live";
import { SandboxPage } from "./sandbox_page";

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
          service_account: { namespace: "agentplane-test", name: "startup-test" },
          status: null,
          kubernetes_grants: [],
          kubernetes_grants_ready: true,
          kubernetes_grant_error: null,
          launch_grants_pending: false,
          deleting: false,
          pod: {
            name: "startup-test",
            namespace: "agentplane-test",
            uid: "test-pod-1",
            deleting: false,
            node_name: "test-node",
            owner_references: [
              {
                api_version: "agents.x-k8s.io/v1beta1",
                kind: "Sandbox",
                name: "startup-test",
                uid: "00000000-0000-4000-8000-000000000001",
                controller: true,
              },
            ],
            status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
          },
        },
        threads: [] as ThreadView[],
        bindings: [],
        action_policy: null,
        watch: { fresh: true, stale_after_seconds: 60, refreshed_seconds_ago: {} },
      },
      health: null,
      stream: { name: "Sandbox startup-test", connection: { phase: "live", since: 0 }, standing: "current" },
    }) satisfies Live<SandboxSnapshot>
);
vi.mock("./live", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./live")>()),
  useLive: () => live,
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  live.snapshot.threads = [];
  (live.snapshot.sandbox as SandboxView).binding = null;
  (live.snapshot.sandbox as SandboxView).kubernetes_grants = [];
  (live.snapshot.sandbox as SandboxView).kubernetes_grants_ready = true;
  (live.snapshot.sandbox as SandboxView).kubernetes_grant_error = null;
  (live.snapshot.sandbox as SandboxView).launch_grants_pending = false;
});
afterAll(() => vi.unstubAllGlobals());

function thread(overrides: Partial<ThreadView> & Pick<ThreadView, "id" | "session_id">): ThreadView {
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
  threadActions: (request: Request) => Promise<Response> = async () => new Response(null, { status: 204 })
): Promise<ReturnType<typeof vi.fn>> {
  fetchMock.mockImplementation((request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/models") {
      return Promise.resolve(
        Response.json({
          models: [{ model: "test-model", display_name: "Test Model", reasoning_efforts: TEST_REASONING_EFFORTS }],
          harnesses: { HARNESS_CLAUDE: ["test-model"], HARNESS_CODEX: [] },
        })
      );
    }
    if (path === "/egress/policies" || path === "/sandboxes/startup-test/egress/decisions") {
      return Promise.resolve(Response.json([]));
    }
    if (path.startsWith("/sandboxes/startup-test/sessions")) return sessions(request);
    if (/^\/threads\/[^/]+\/(un)?archive$/.test(path)) return threadActions(request);
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const onOpenThread = vi.fn();
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <SandboxPage name="startup-test" onBack={vi.fn()} onOpenThread={onOpenThread} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
  return onOpenThread;
}

function newSession(): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((node) =>
    /New session|Creating session/.test(node.textContent ?? "")
  );
  if (!button) throw new Error("Missing new session button");
  return button;
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

it("preserves a Sandbox reasoning default through model loading and submits it on later Thread launch", async () => {
  (live.snapshot.sandbox as SandboxView).binding = {
    bootstrap: "",
    session_defaults: { harness: "HARNESS_CLAUDE", model: "test-model", reasoning_effort: "high" },
  };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  await render(sessions);
  expect(labeledInput("Reasoning effort").value).toBe("high");
  await act(async () => newSession().click());
  const sent = sessions.mock.calls.find(([request]) => request.method === "POST")?.[0];
  expect(sent).toBeDefined();
  expect((await sent?.json()).spec.reasoningEffort).toBe("high");
});

it("uses the bound working directory template and setup script for later Threads", async () => {
  (live.snapshot.sandbox as SandboxView).binding = {
    bootstrap: "",
    session_defaults: {
      harness: "HARNESS_CLAUDE",
      model: "test-model",
      cwd: "/state/custom/{session_id}/work",
      setup_script: "printf 'ready\\n'",
    },
  };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  await render(sessions);
  await act(async () => newSession().click());
  const sent = sessions.mock.calls.find(([request]) => request.method === "POST")?.[0];
  const body = await sent?.json();
  expect(body.spec.cwd).toMatch(/^\/state\/custom\/s-[^/]+\/work$/);
  expect(body.setup_script).toBe("printf 'ready\\n'");
});

it("shows an unspecified legacy setup as absent and a completed setup as complete", async () => {
  const spec = { harness: "HARNESS_CLAUDE", cwd: "/work", model: "test-model" };
  await render(async () =>
    Response.json([
      { sessionId: "legacy", spec, harnessState: "HARNESS_STATE_STOPPED" },
      { sessionId: "prepared", spec, harnessState: "HARNESS_STATE_RUNNING", setupState: "SETUP_STATE_SUCCEEDED" },
    ])
  );
  await vi.waitFor(() => {
    const rows = [...container.querySelectorAll("tbody tr")];
    expect(rows.find((row) => row.textContent?.includes("legacy"))?.children[2]?.textContent).toBe("—");
    expect(rows.find((row) => row.textContent?.includes("prepared"))?.children[2]?.textContent).toBe("Complete");
  });
});

it("lets a later Thread override the Sandbox reasoning default locally", async () => {
  (live.snapshot.sandbox as SandboxView).binding = {
    bootstrap: "",
    session_defaults: { harness: "HARNESS_CLAUDE", model: "test-model", reasoning_effort: "high" },
  };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  await render(sessions);
  await choose("Reasoning effort", "medium");
  await act(async () => newSession().click());
  const sent = sessions.mock.calls.find(([request]) => request.method === "POST")?.[0];
  expect((await sent?.json()).spec.reasoningEffort).toBe("medium");
  expect((live.snapshot.sandbox as SandboxView).binding?.session_defaults?.reasoning_effort).toBe("high");
});

it("shows the selected Kubernetes grant scope, role, and application error", async () => {
  (live.snapshot.sandbox as SandboxView).launch_grants_pending = true;
  (live.snapshot.sandbox as SandboxView).kubernetes_grants = [
    {
      name: "workspace-read",
      grant: {
        kind: "RoleBinding",
        namespace: "agentplane-test",
        role_ref: { kind: "Role", name: "workspace-reader" },
      },
    },
  ];
  (live.snapshot.sandbox as SandboxView).kubernetes_grants_ready = false;
  (live.snapshot.sandbox as SandboxView).kubernetes_grant_error = "binding controller is waiting";
  await render(async () => Response.json([]));
  const statusTab = [...container.querySelectorAll<HTMLButtonElement>('button[role="tab"]')].find(
    (tab) => tab.textContent === "Status"
  );
  if (!statusTab) throw new Error("Missing Status tab");
  await act(async () => statusTab.click());
  expect(container.textContent).toContain("Error");
  expect(container.textContent).toContain("Launch or Kubernetes grants are not ready; sessions cannot start yet.");
  expect(container.textContent).toContain("workspace-read · RoleBinding · namespace agentplane-test");
  expect(container.textContent).toContain("Role/workspace-reader");
  expect(container.textContent).toContain("binding controller is waiting");
});

/** Mantine portals a Menu's dropdown onto `document.body`, so its items live outside `container`. */
function menuItem(text: string): HTMLElement {
  const item = [...document.querySelectorAll('[role="menuitem"]')].find((node) => node.textContent === text);
  if (!(item instanceof HTMLElement)) throw new Error(`Missing menu item ${text}`);
  return item;
}

it.each([409, 503])("recovers from runner HTTP %s without a reload", async (status) => {
  vi.useFakeTimers();
  const sessions = vi
    .fn<(request: Request) => Promise<Response>>()
    .mockResolvedValueOnce(Response.json({ detail: "runner not available" }, { status }))
    .mockResolvedValueOnce(Response.json([{ sessionId: "existing-session" }]));
  await render(sessions);
  expect(container.querySelector('[role="status"]')?.textContent).toContain("Waiting for the sandbox runner");
  expect(newSession().disabled).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(container.textContent).toContain("existing-session");
  expect(container.querySelector('[role="status"]')).toBeNull();
  expect(newSession().disabled).toBe(false);
});

it("reports non-transient session-list failures instead of treating them as startup", async () => {
  await render(async () => Response.json({ detail: "invalid runner response" }, { status: 500 }));
  expect(container.textContent).toContain("invalid runner response");
  expect(container.textContent).not.toContain("Waiting for the sandbox runner");
});

it("shows creation progress, prevents duplicate clicks, and restores the button after failure", async () => {
  let fail!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    fail = resolve;
  });
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    request.method === "GET" ? Promise.resolve(Response.json([])) : pending
  );
  const onOpenThread = await render(sessions);
  await act(async () => newSession().click());
  expect(newSession().textContent).toContain("Creating session");
  expect(newSession().disabled).toBe(true);
  await act(async () => newSession().click());
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  await act(async () => fail(Response.json({ detail: "session refused" }, { status: 422 })));
  expect(newSession().disabled).toBe(false);
  expect(container.textContent).toContain("session refused");
  expect(onOpenThread).not.toHaveBeenCalled();
});

it("hides an archived thread's session by default, reveals it via Show archived, and unarchives it", async () => {
  live.snapshot.threads = [thread({ id: "test-thread-1", session_id: "existing-session", archived: true })];
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
  if (!menuButton) throw new Error("Missing per-session actions menu");
  await act(async () => menuButton.click());
  await act(async () => menuItem("Unarchive").click());

  expect(archiveRequests.map((request) => [request.method, new URL(request.url).pathname])).toEqual([
    ["POST", "/threads/test-thread-1/unarchive"],
  ]);
});

it("disables archiving a thread while its harness is running", async () => {
  live.snapshot.threads = [thread({ id: "test-thread-1", session_id: "existing-session" })];
  await render(async () =>
    Response.json([
      {
        sessionId: "existing-session",
        spec: {},
        lastCursor: "0",
        harnessState: "HARNESS_STATE_RUNNING",
      },
    ])
  );
  const menuButton = [...container.querySelectorAll("button")].find(
    (node) => node.getAttribute("aria-label") === "More actions for existing-session"
  );
  if (!menuButton) throw new Error("Missing per-session actions menu");
  await act(async () => menuButton.click());
  const archiveItem = menuItem("Stop harness before archiving");
  expect(archiveItem).toBeInstanceOf(HTMLButtonElement);
  expect((archiveItem as HTMLButtonElement).disabled).toBe(true);
  await act(async () => archiveItem.click());
  expect(
    fetchMock.mock.calls.some(([request]) => new URL((request as Request).url).pathname.endsWith("/archive"))
  ).toBe(false);
});
