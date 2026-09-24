// @vitest-environment happy-dom
import { create, equals, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import type { ThreadView } from "./client";
import type { Live, SandboxesSnapshot } from "./live";
import { LocalCommands } from "./local_commands";
import { ProjectedSession, pruneCommandErrors } from "./projected_session";
import { ThreadSyncContext, type ThreadSync, type ThreadWindow } from "./thread_sync";

const fetchMock = vi.hoisted(() => {
  const fetch = vi.fn<(request: Request) => Promise<Response>>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
});

const live = vi.hoisted(
  () =>
    ({
      snapshot: {
        sandboxes: [
          {
            name: "redelivery-test",
            uid: "00000000-0000-4000-8000-00000000c0de",
            state: "running",
            created_at: "2026-01-01T00:00:00Z",
            operating_mode: "Running",
            service_account: { namespace: "agentplane-test", name: "redelivery-test" },
            conditions: [],
          },
        ],
        watch: { fresh: true, stale_after_seconds: 60, refreshed_seconds_ago: {} },
      },
      health: { fresh: true, stale_after_seconds: 60, refreshed_seconds_ago: {} },
      connection: "connected",
    }) satisfies Live<SandboxesSnapshot>
);
vi.mock("./live", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./live")>()),
  useLive: () => live,
}));

const THREAD: ThreadView = {
  id: "10000000-0000-4000-8000-00000000c0de",
  sandbox: "redelivery-test",
  session_id: "session-test",
  harness: "HARNESS_CLAUDE",
  model: "test-model",
  cwd: "/test-workspace",
  created_at: "2026-01-01T00:00:00Z",
  name: "Test thread",
  archived: false,
  last_cursor: 0,
  harness_state: "HARNESS_STATE_RUNNING",
};

// A caught-up window without rows: the page learns of admission only from the command POST's answer.
const WINDOW: ThreadWindow = {
  rows: [],
  caughtUp: true,
  olderAvailable: false,
  loadOlder: () => {},
  error: null,
  refresh: () => {},
};
const SYNC: ThreadSync = {
  Thread: ({ children }) => <>{children}</>,
  useThread: () => ({ window: WINDOW, error: null }),
  useCommandRows: () => [],
  usePayload: () => ({ body: null, error: null, retry: () => {} }),
};

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const roots: ReturnType<typeof createRoot>[] = [];
let container: HTMLDivElement;
let posted: Command[];
let deadlines: AbortController[];
const answerCommand = vi.fn<(command: Command, request: Request) => Promise<Response>>();

beforeEach(() => {
  localStorage.clear();
  posted = [];
  deadlines = [];
  // Each command POST's deadline, fired by the test rather than by elapsed time.
  vi.spyOn(AbortSignal, "timeout").mockImplementation(() => {
    const deadline = new AbortController();
    deadlines.push(deadline);
    return deadline.signal;
  });
  fetchMock.mockImplementation(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === `/threads/${THREAD.id}`) return Response.json(THREAD);
    if (request.method === "GET" && path === "/models") {
      return Response.json({ HARNESS_CLAUDE: ["test-model"], HARNESS_CODEX: [] });
    }
    if (request.method === "POST" && path === `/threads/${THREAD.id}/commands`) {
      const command = fromJson(CommandSchema, (await request.json()) as JsonValue);
      posted.push(command);
      return answerCommand(command, request);
    }
    throw new Error(`Unexpected request: ${request.method} ${path}`);
  });
});
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
  container?.remove();
  vi.restoreAllMocks();
  answerCommand.mockReset();
});

function message(commandId: string): Command {
  return create(CommandSchema, {
    commandId,
    operation: { case: "submitInput", value: { text: `Test input ${commandId}` } },
  });
}

function admission(command: Command): EventEntry {
  return create(EventEntrySchema, {
    cursor: 7n,
    origin: { sourceId: "test-runner", sequence: 7n },
    event: { observation: { case: "commandAdmitted", value: { command } } },
  });
}

async function admit(command: Command): Promise<Response> {
  return Response.json(toJson(EventEntrySchema, admission(command)));
}

/** Never answers; rejects the way fetch does once the request's signal aborts. */
function hang(_command: Command, request: Request): Promise<Response> {
  return new Promise((_resolve, reject) =>
    request.signal.addEventListener("abort", () => reject(request.signal.reason), { once: true })
  );
}

async function render(): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <ThreadSyncContext.Provider value={SYNC}>
          <ProjectedSession threadId={THREAD.id} onBack={() => {}} />
        </ThreadSyncContext.Provider>
      </MantineProvider>
    )
  );
}

function pendingRow(commandId: string): HTMLElement {
  const row = container.querySelector<HTMLElement>(`[data-command-id="${commandId}"]`);
  if (!row) throw new Error(`Missing pending command ${commandId}`);
  return row;
}

function retry(commandId: string): HTMLButtonElement {
  const button = [...pendingRow(commandId).querySelectorAll("button")].find((node) => node.textContent === "Retry");
  if (!button) throw new Error(`Missing Retry for ${commandId}`);
  return button;
}

it("drops request errors after their local commands are dismissed", () => {
  const errors = new Map([
    ["dismissed", "connection lost"],
    ["pending", "request timed out"],
  ]);

  expect(pruneCommandErrors(errors, new Set(["pending"]))).toEqual(new Map([["pending", "request timed out"]]));
  expect(pruneCommandErrors(errors, new Set(errors.keys()))).toBe(errors);
});

it("delivers a command an earlier page retained without the operator acting, and keeps its admission", async () => {
  new LocalCommands(THREAD.id).remember(message("retained-unadmitted"));
  answerCommand.mockImplementation(admit);
  await render();
  expect(posted.map((command) => command.commandId)).toEqual(["retained-unadmitted"]);
  expect(pendingRow("retained-unadmitted").textContent).toContain("Saved · awaiting effect");
  const [retained] = new LocalCommands(THREAD.id).getSnapshot().commands;
  expect(equals(EventEntrySchema, retained.admission!, admission(message("retained-unadmitted")))).toBe(true);
});

it("does not send a retained command whose admission it already holds", async () => {
  const store = new LocalCommands(THREAD.id);
  store.remember(message("retained-admitted"));
  store.acknowledge(message("retained-admitted"), admission(message("retained-admitted")));
  // Its unadmitted sibling being sent shows the mount's delivery ran.
  store.remember(message("retained-unadmitted"));
  answerCommand.mockImplementation(admit);
  await render();
  expect(posted.map((command) => command.commandId)).toEqual(["retained-unadmitted"]);
  expect(pendingRow("retained-admitted").textContent).toContain("Saved · awaiting effect");
});

it("shows a command POST that outlives its deadline as a failed attempt the operator can retry", async () => {
  new LocalCommands(THREAD.id).remember(message("hung"));
  answerCommand.mockImplementationOnce(hang).mockImplementationOnce(admit);
  await render();
  expect(posted).toHaveLength(1);
  expect(pendingRow("hung").textContent).toContain("Saved locally · awaiting admission");

  await act(async () => deadlines[0].abort(new DOMException("signal timed out", "TimeoutError")));
  expect(pendingRow("hung").textContent).toContain("signal timed out");
  expect(posted).toHaveLength(1);

  await act(async () => retry("hung").click());
  expect(posted.map((command) => command.commandId)).toEqual(["hung", "hung"]);
  expect(pendingRow("hung").textContent).toContain("Saved · awaiting effect");
  expect(pendingRow("hung").textContent).not.toContain("signal timed out");
});

it("delivers a failed command again when the browser comes back online", async () => {
  new LocalCommands(THREAD.id).remember(message("offline"));
  answerCommand
    .mockImplementationOnce(async () =>
      Response.json({ detail: "the sandbox's runner is not answering" }, { status: 503 })
    )
    .mockImplementationOnce(admit);
  await render();
  expect(pendingRow("offline").textContent).toContain("the sandbox's runner is not answering");
  expect(posted).toHaveLength(1);

  await act(async () => window.dispatchEvent(new Event("online")));
  expect(posted.map((command) => command.commandId)).toEqual(["offline", "offline"]);
  expect(pendingRow("offline").textContent).toContain("Saved · awaiting effect");
});
