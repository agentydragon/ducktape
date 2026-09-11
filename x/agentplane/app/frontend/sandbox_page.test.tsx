// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, afterEach, expect, it, vi } from "vitest";

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
          archived: false,
          state: "running",
          created_at: "2026-01-01T00:00:00Z",
          operating_mode: "Running",
          conditions: [],
        },
        threads: [],
        bindings: [],
        watch: { fresh: true, stale_after_seconds: 60, refreshed_seconds_ago: {} },
      },
      connection: "connected",
      health: null,
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
});
afterAll(() => vi.unstubAllGlobals());

async function render(sessions: (request: Request) => Promise<Response>): Promise<ReturnType<typeof vi.fn>> {
  fetchMock.mockImplementation((request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/models") {
      return Promise.resolve(Response.json({ claude: ["test-model"], codex: [] }));
    }
    if (path === "/egress/policies" || path === "/sandboxes/startup-test/egress/decisions") {
      return Promise.resolve(Response.json([]));
    }
    if (path.startsWith("/sandboxes/startup-test/sessions")) return sessions(request);
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const onOpenSession = vi.fn();
  await act(async () =>
    root.render(
      <MantineProvider>
        <MemoryRouter>
          <SandboxPage name="startup-test" onBack={vi.fn()} onOpenSession={onOpenSession} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
  return onOpenSession;
}

function newSession(): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((node) =>
    /New session|Creating session/.test(node.textContent ?? "")
  );
  if (!button) throw new Error("Missing new session button");
  return button;
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
  const onOpenSession = await render(sessions);
  await act(async () => newSession().click());
  expect(newSession().textContent).toContain("Creating session");
  expect(newSession().disabled).toBe(true);
  await act(async () => newSession().click());
  expect(sessions.mock.calls.filter(([request]) => request.method === "POST")).toHaveLength(1);
  await act(async () => fail(Response.json({ detail: "session refused" }, { status: 422 })));
  expect(newSession().disabled).toBe(false);
  expect(container.textContent).toContain("session refused");
  expect(onOpenSession).not.toHaveBeenCalled();
});
