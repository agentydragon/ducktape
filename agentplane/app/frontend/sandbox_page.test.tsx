// @vitest-environment happy-dom
import { act, screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { TEST_REASONING_EFFORTS, testModelCatalog } from "./test_model_catalog";
import { afterAll, afterEach, expect, it, vi, type Mock } from "vitest";

import type { SandboxView, ThreadView } from "./client";
import type * as LiveModule from "./live";
import type { Live, SandboxSnapshot } from "./live";
import { SandboxPage } from "./sandbox_page";
import { renderInMantine } from "./testing_library";

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
  ...(await importOriginal<typeof LiveModule>()),
  useLive: () => live,
}));

afterEach(() => {
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

function render(
  sessions: (request: Request) => Promise<Response>,
  threadActions: (request: Request) => Promise<Response> = async () => new Response(null, { status: 204 }),
  claudePaused = false
): ReturnType<typeof renderInMantine> & { onOpenThread: ReturnType<typeof vi.fn> } {
  fetchMock.mockImplementation((request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/models") {
      const models = [{ model: "test-model", display_name: "Test Model", reasoning_efforts: TEST_REASONING_EFFORTS }];
      return Promise.resolve(Response.json(testModelCatalog(claudePaused ? [] : models, claudePaused ? models : [])));
    }
    if (path === "/egress/policies" || path === "/sandboxes/startup-test/egress/decisions") {
      return Promise.resolve(Response.json([]));
    }
    if (path.startsWith("/sandboxes/startup-test/sessions")) return sessions(request);
    if (/^\/threads\/[^/]+\/(un)?archive$/.test(path)) return threadActions(request);
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  });
  const onOpenThread = vi.fn();
  const rendered = renderInMantine(
    <MemoryRouter>
      <SandboxPage name="startup-test" onBack={vi.fn()} onOpenThread={onOpenThread} />
    </MemoryRouter>
  );
  return Object.assign(rendered, { onOpenThread });
}

const newSession = (): HTMLElement => screen.getByRole("button", { name: /New session|Creating session/ });

async function choose(user: UserEvent, label: string, value: string): Promise<void> {
  await user.click(await screen.findByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: value }));
}

/** The JSON body of the first POST the page made to the sessions endpoint. */
async function postedBody(sessions: Mock<(request: Request) => Promise<Response>>) {
  const posted = sessions.mock.calls.find(([request]) => request.method === "POST")?.[0];
  if (posted === undefined) throw new Error("no POST reached the sessions endpoint");
  return await posted.json();
}

it("preserves a Sandbox reasoning default through model loading and submits it on later Thread launch", async () => {
  (live.snapshot.sandbox as SandboxView).binding = {
    bootstrap: "",
    session_defaults: { harness: "HARNESS_CLAUDE", model: "test-model", reasoning_effort: "high" },
  };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  const { user } = render(sessions);
  expect(await screen.findByRole("combobox", { name: "Reasoning effort" })).toHaveValue("high");
  await user.click(newSession());
  expect((await postedBody(sessions)).spec.reasoningEffort).toBe("high");
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
  const { user } = render(sessions);
  await waitFor(() => expect(newSession()).toBeEnabled());
  await user.click(newSession());
  const body = await postedBody(sessions);
  expect(body.spec.cwd).toMatch(/^\/state\/custom\/s-[^/]+\/work$/);
  expect(body.setup_script).toBe("printf 'ready\\n'");
});

it("shows an unspecified legacy setup as absent and a completed setup as complete", async () => {
  const spec = { harness: "HARNESS_CLAUDE", cwd: "/work", model: "test-model" };
  render(async () =>
    Response.json([
      { sessionId: "legacy", spec, harnessState: "HARNESS_STATE_STOPPED" },
      { sessionId: "prepared", spec, harnessState: "HARNESS_STATE_RUNNING", setupState: "SETUP_STATE_SUCCEEDED" },
    ])
  );
  // The Setup column is the third cell of a row.
  const setup = async (session: string) =>
    within(await screen.findByRole("row", { name: new RegExp(session) })).getAllByRole("cell")[2];
  expect(await setup("legacy")).toHaveTextContent(/^—$/);
  expect(await setup("prepared")).toHaveTextContent(/^Complete$/);
});

it("lets a later Thread override the Sandbox reasoning default locally", async () => {
  (live.snapshot.sandbox as SandboxView).binding = {
    bootstrap: "",
    session_defaults: { harness: "HARNESS_CLAUDE", model: "test-model", reasoning_effort: "high" },
  };
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    Promise.resolve(request.method === "GET" ? Response.json([]) : Response.json({ detail: "stop" }, { status: 422 }))
  );
  const { user } = render(sessions);
  await choose(user, "Reasoning effort", "medium");
  await user.click(newSession());
  expect((await postedBody(sessions)).spec.reasoningEffort).toBe("medium");
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
  const { container, user } = render(async () => Response.json([]));
  await user.click(await screen.findByRole("tab", { name: "Status" }));
  expect(container).toHaveTextContent("Error");
  expect(container).toHaveTextContent("Launch or Kubernetes grants are not ready; sessions cannot start yet.");
  expect(container).toHaveTextContent("workspace-read · RoleBinding · namespace agentplane-test");
  expect(container).toHaveTextContent("Role/workspace-reader");
  expect(container).toHaveTextContent("binding controller is waiting");
});

it.each([409, 503])("recovers from runner HTTP %s without a reload", async (status) => {
  vi.useFakeTimers();
  const sessions = vi
    .fn<(request: Request) => Promise<Response>>()
    .mockResolvedValueOnce(Response.json({ detail: "runner not available" }, { status }))
    .mockResolvedValueOnce(Response.json([{ sessionId: "existing-session" }]));
  render(sessions);
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Waiting for the sandbox runner"));
  expect(newSession()).toBeDisabled();
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(await screen.findByText("existing-session")).toBeInTheDocument();
  expect(screen.queryByRole("status")).toBeNull();
  expect(newSession()).toBeEnabled();
});

it("reports non-transient session-list failures instead of treating them as startup", async () => {
  const { container } = render(async () => Response.json({ detail: "invalid runner response" }, { status: 500 }));
  expect(await screen.findByText(/invalid runner response/)).toBeInTheDocument();
  expect(container).not.toHaveTextContent("Waiting for the sandbox runner");
});

it("shows creation progress, prevents duplicate clicks, and restores the button after failure", async () => {
  let fail!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    fail = resolve;
  });
  const sessions = vi.fn<(request: Request) => Promise<Response>>((request) =>
    request.method === "GET" ? Promise.resolve(Response.json([])) : pending
  );
  const { onOpenThread, user } = render(sessions);
  await waitFor(() => expect(newSession()).toBeEnabled());
  await user.click(newSession());
  expect(newSession()).toHaveTextContent("Creating session");
  expect(newSession()).toBeDisabled();
  await user.click(newSession());
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  await act(async () => fail(Response.json({ detail: "session refused" }, { status: 422 })));
  expect(newSession()).toBeEnabled();
  expect(await screen.findByText(/session refused/)).toBeInTheDocument();
  expect(onOpenThread).not.toHaveBeenCalled();
});

it("hides an archived thread's session by default, reveals it via Show archived, and unarchives it", async () => {
  live.snapshot.threads = [thread({ id: "test-thread-1", session_id: "existing-session", archived: true })];
  const archiveRequests: Request[] = [];
  const { container, user } = render(
    async () => Response.json([{ sessionId: "existing-session" }]),
    async (request) => {
      archiveRequests.push(request);
      return new Response(null, { status: 204 });
    }
  );
  await screen.findByRole("switch", { name: "Show archived" });
  expect(container).not.toHaveTextContent("existing-session");

  await user.click(screen.getByRole("switch", { name: "Show archived" }));
  expect(await screen.findByText("existing-session")).toBeInTheDocument();

  // Mantine portals a Menu's dropdown onto `document.body`, which a `screen` query reaches.
  await user.click(screen.getByRole("button", { name: "More actions for existing-session" }));
  await user.click(await screen.findByRole("menuitem", { name: "Unarchive" }));

  expect(archiveRequests.map((request) => [request.method, new URL(request.url).pathname])).toEqual([
    ["POST", "/threads/test-thread-1/unarchive"],
  ]);
});

it("disables archiving a thread while its harness is running", async () => {
  live.snapshot.threads = [thread({ id: "test-thread-1", session_id: "existing-session" })];
  const { user } = render(async () =>
    Response.json([
      {
        sessionId: "existing-session",
        spec: {},
        lastCursor: "0",
        harnessState: "HARNESS_STATE_RUNNING",
      },
    ])
  );
  await user.click(await screen.findByRole("button", { name: "More actions for existing-session" }));
  const archiveItem = await screen.findByRole("menuitem", { name: "Stop harness before archiving" });
  expect(archiveItem).toBeDisabled();
  await user.click(archiveItem);
  expect(
    fetchMock.mock.calls.some(([request]) => new URL((request as Request).url).pathname.endsWith("/archive"))
  ).toBe(false);
});

it("defaults new sessions to an offered harness while retaining existing Claude threads", async () => {
  live.snapshot.threads = [thread({ id: "old-claude", session_id: "old-session", name: "Existing Claude" })];
  const sessions = vi.fn<(request: Request) => Promise<Response>>(async (request) =>
    request.method === "GET"
      ? Response.json([{ sessionId: "old-session", spec: { harness: "HARNESS_CLAUDE" } }])
      : Response.json({ detail: "Launch recorded" }, { status: 503 })
  );
  const { user } = render(sessions, undefined, true);
  expect(await screen.findByText("Existing Claude")).toBeInTheDocument();
  const harness = screen.getByRole("combobox", { name: "Harness" });
  await waitFor(() => expect(harness).toHaveValue("Codex"));
  await user.click(harness);
  const claude = screen.getByRole("option", { name: "Claude (no models offered)" });
  // Mantine marks a disabled option only with this attribute, not `aria-disabled`.
  expect(claude).toHaveAttribute("data-combobox-disabled");
  await user.click(claude);
  expect(harness).toHaveValue("Codex");
  await user.click(harness);
  await user.click(newSession());
  expect((await postedBody(sessions)).spec.harness).toBe("HARNESS_CODEX");
});
