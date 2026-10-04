// @vitest-environment happy-dom

import { create, equals, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { TEST_REASONING_EFFORTS } from "../test_model_catalog";
import { MantineProvider } from "@mantine/core";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../../protocol/event_log_pb";
import { EventSchema, ItemKind, RecoveryDisposition, TurnStatus } from "../../../protocol/event_pb";
import { command, getThread, models, resumeThread, type SandboxView, type ThreadView } from "../client";
import { historyRows, rowKey } from "./history_rows";
import { ThreadsLiveProvider } from "../live";
import { LocalCommands } from "./local_commands";
import { STREAMING_CURSOR } from "../markdown";
import { HistoryRowView, ProjectedSession } from "./projected_session";
import { pruneCommandErrors } from "./thread_commands";
import { EntityCard } from "./thread_cards";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import { DEGRADED_AFTER_MS, STALE_AFTER_MS } from "../stream_status";
import { THREAD_STATUS_MARKS } from "../status_mark";
import { testItem } from "./thread_entity_fixture";
import {
  ThreadSyncContext,
  type PayloadRef,
  type ThreadEntity,
  type ThreadState,
  type ThreadSync,
} from "./thread_sync";
import { TopbarContext } from "../topbar";

vi.mock("../client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../client")>()),
  command: vi.fn(),
  getThread: vi.fn(),
  models: vi.fn(),
  resumeThread: vi.fn(),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{
  root: ReturnType<typeof createRoot>;
  container: HTMLDivElement;
  topbarTitle?: HTMLDivElement;
  topbarActions?: HTMLDivElement;
}> = [];
const THREAD: ThreadView = {
  id: "10000000-0000-4000-8000-000000000001",
  sandbox: "composer-test",
  session_id: "session-test",
  harness: "HARNESS_CLAUDE",
  model: "test-model",
  cwd: "/test-workspace",
  created_at: "2026-01-01T00:00:00Z",
  name: "Test thread",
  archived: false,
  last_cursor: 1,
  last_event_at: null,
  harness_state: "HARNESS_STATE_RUNNING",
  reasoning_effort: "low",
};
let favicon: HTMLLinkElement;

// What the sandbox inventory stream reports -- nothing at all while `sandboxes` is null -- and
// whether it then drops. By default the thread's sandbox is running on a current inventory, so its
// controls are live.
type Inventory = SandboxView[] | null;
function inventorySandbox(operating_mode: "Running" | "Suspended" = "Running"): SandboxView {
  const uid = "20000000-0000-4000-8000-000000000001";
  return {
    name: THREAD.sandbox,
    uid,
    namespace: "agentplane-test",
    created_at: "2026-01-01T00:00:00Z",
    operating_mode,
    status: null,
    service_account: { namespace: "agentplane-test", name: THREAD.sandbox },
    kubernetes_grants: [],
    kubernetes_grants_ready: true,
    kubernetes_grant_error: null,
    launch_grants_pending: false,
    deleting: false,
    pod:
      operating_mode === "Suspended"
        ? null
        : {
            name: THREAD.sandbox,
            namespace: "agentplane-test",
            uid: "test-pod-1",
            deleting: false,
            node_name: "test-node",
            owner_references: [
              { api_version: "agents.x-k8s.io/v1beta1", kind: "Sandbox", name: THREAD.sandbox, uid, controller: true },
            ],
            status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
          },
  };
}
let sandboxes: Inventory = [];
let inventoryFresh = true;
let inventoryDrops = false;
let sharedFeed: "active" | "ended" | "failed" = "active";
let sharedThread: Partial<ThreadView> = {};

beforeEach(() => {
  document.title = "Agentplane";
  favicon = document.createElement("link");
  favicon.id = "agentplane-favicon";
  favicon.rel = "icon";
  favicon.href = "/favicon.svg";
  document.head.append(favicon);
  localStorage.clear();
  sandboxes = [inventorySandbox()];
  inventoryFresh = true;
  inventoryDrops = false;
  sharedFeed = "active";
  sharedThread = {};
  vi.mocked(getThread).mockResolvedValue(THREAD);
  vi.mocked(models).mockResolvedValue({
    models: [{ model: "test-model", display_name: "Test Model", reasoning_efforts: TEST_REASONING_EFFORTS }],
    harnesses: { HARNESS_CLAUDE: ["test-model"], HARNESS_CODEX: [] },
  });
  vi.mocked(command).mockReturnValue(new Promise(() => {}));
  vi.mocked(resumeThread).mockResolvedValue({} as never);
  vi.stubGlobal(
    "EventSource",
    class extends EventTarget {
      // A drop is the network's, which the browser retries: the source stays CONNECTING.
      readyState = 0;
      constructor(url: string) {
        super();
        queueMicrotask(() => {
          if (sandboxes === null) return;
          this.dispatchEvent(
            new MessageEvent("snapshot", {
              data: JSON.stringify({
                sandboxes,
                ...(url === "/live/threads"
                  ? {
                      threads: [
                        { ...THREAD, harness_state: "HARNESS_STATE_RUNNING", feed_status: sharedFeed, ...sharedThread },
                      ],
                      updates_connected: true,
                    }
                  : {}),
                watch: {
                  fresh: inventoryFresh,
                  stale_after_seconds: 90,
                  refreshed_seconds_ago: { sandboxes: inventoryFresh ? 0 : 2400 },
                },
              }),
            })
          );
          if (inventoryDrops) this.dispatchEvent(new Event("error"));
        });
      }
      close(): void {}
    }
  );
});

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
  favicon.remove();
  document.title = "Agentplane";
});

function viewState({
  harness = "running",
  status = "active",
  model = "test-model",
  activeTurn = null,
}: {
  harness?: string | null;
  status?: "active" | "ended" | "failed";
  model?: string | null;
  activeTurn?: string | null;
} = {}): ThreadEntity {
  return {
    threadId: THREAD.id,
    projectionEpoch: "test-epoch",
    entityKind: "view_state",
    entityId: "current",
    entityIndex: "0",
    cursor: "1",
    revisionCursor: "1",
    pending: false,
    turnId: null,
    state: {
      controls: {
        applied_model: model,
        applied_reasoning_effort: null,
        active_turn_id: activeTurn,
        harness_state: harness,
      },
      operational: { status, last_verified_cursor: "1", feed_error: null },
    },
    textRef: null,
    argumentsRef: null,
    outputRef: null,
    inputRef: null,
  };
}

function threadState({
  rows = [viewState()],
  caughtUp = true,
  reconnectingFor = null,
  windowError = null,
  error = null,
}: {
  rows?: ThreadEntity[];
  caughtUp?: boolean;
  /** How long the thread's reads have been failing, if they are. */
  reconnectingFor?: number | null;
  windowError?: string | null;
  error?: string | null;
} = {}): ThreadState {
  return {
    window: {
      rows,
      caughtUp,
      olderAvailable: false,
      loadingOlder: false,
      loadOlder: () => {},
      connection:
        reconnectingFor === null
          ? { phase: "live", since: Date.now() }
          : { phase: "reconnecting", since: Date.now() - reconnectingFor, attempt: 1, lastError: "HTTP 503" },
      error: windowError,
      refresh: () => {},
    },
    error,
  };
}

async function render(state: ThreadState = threadState()): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  // The real shell topbar (app.tsx) isn't mounted here, so ProjectedSession's title/menu need
  // somewhere to portal into. Left unattached until after the initial render: createRoot's first
  // commit clears container's pre-existing children, which would tear these back out.
  const topbarTitle = document.createElement("div");
  const topbarActions = document.createElement("div");
  const root = createRoot(container);
  mounted.push({ root, container, topbarTitle, topbarActions });
  await act(async () => {
    const content = page(state, topbarTitle, topbarActions);
    root.render(<ThreadsLiveProvider>{content}</ThreadsLiveProvider>);
  });
  container.append(topbarTitle, topbarActions);
  return container;
}

function page(state: ThreadState, topbarTitle: HTMLDivElement, topbarActions: HTMLDivElement): JSX.Element {
  const sync: ThreadSync = {
    Thread: ({ children }) => <>{children}</>,
    useThread: () => state,
    useCommandRows: () => [],
    usePayload: () => ({ body: null, error: null, retry: () => {} }),
  };
  return (
    <MantineProvider env="test">
      <ThreadSyncContext.Provider value={sync}>
        <TopbarContext.Provider value={{ title: topbarTitle, actions: topbarActions }}>
          <ProjectedSession threadId={THREAD.id} />
        </TopbarContext.Provider>
      </ThreadSyncContext.Provider>
    </MantineProvider>
  );
}

async function rerender(container: HTMLDivElement, state: ThreadState): Promise<void> {
  const current = mounted.find((entry) => entry.container === container);
  const topbarTitle = current?.topbarTitle;
  const topbarActions = current?.topbarActions;
  if (!current || !topbarTitle || !topbarActions) throw new Error("Missing mounted thread page");
  await act(async () =>
    current.root.render(<ThreadsLiveProvider>{page(state, topbarTitle, topbarActions)}</ThreadsLiveProvider>)
  );
  container.append(topbarTitle, topbarActions);
}

function composer(container: HTMLDivElement): HTMLTextAreaElement {
  const field = container.querySelector("textarea");
  if (!field) throw new Error("Missing composer");
  return field;
}

async function type(field: HTMLTextAreaElement, text: string): Promise<void> {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(field, text);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function press(field: HTMLTextAreaElement, init: KeyboardEventInit): Promise<void> {
  await act(async () => {
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, ...init }));
  });
}

function button(container: HTMLDivElement, label: string): HTMLButtonElement {
  const found = container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (!found) throw new Error(`Missing ${label}`);
  return found;
}

async function openMenuItem(container: HTMLDivElement, text: string): Promise<HTMLButtonElement> {
  await act(async () => button(container, "More").click());
  const item = [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(
    (candidate) => candidate.textContent === text
  );
  if (!item) throw new Error(`Missing menu item ${text}`);
  return item;
}

function sentOperations(): unknown[] {
  return vi.mocked(command).mock.calls.map(([, value]) => value.operation);
}

it("drops request errors after their local commands are dismissed", () => {
  const errors = new Map([
    ["dismissed", "connection lost"],
    ["pending", "request timed out"],
  ]);

  expect(pruneCommandErrors(errors, new Set(["pending"]))).toEqual(new Map([["pending", "request timed out"]]));
  expect(pruneCommandErrors(errors, new Set(errors.keys()))).toBe(errors);
});

it.each<KeyboardEventInit>([{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }])(
  "inserts a newline at the caret on Enter with %o, without sending",
  async (modifier) => {
    const field = composer(await render());
    await type(field, "helloworld");
    field.setSelectionRange(5, 5);
    await press(field, modifier);
    // The caret is put back on the frame after the controlled value lands.
    await act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    expect(field.value).toBe("hello\nworld");
    expect([field.selectionStart, field.selectionEnd]).toEqual([6, 6]);
    expect(command).not.toHaveBeenCalled();
  }
);

it("sends the draft on Enter and clears it", async () => {
  const container = await render();
  const field = composer(container);
  await type(field, "hello");
  await press(field, {});
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "hello" } }]);
  expect(field.value).toBe("");
  const bubble = container.querySelector<HTMLElement>('.agentplane-user-bubble[data-message-phase="local"]');
  expect(bubble?.querySelector(".agentplane-verbatim")?.textContent).toBe("hello");
  expect(bubble?.textContent).toContain("Saved locally · awaiting admission");
  const messageRow = bubble?.parentElement;
  expect(messageRow?.firstElementChild?.textContent).toBe("Retry");
  expect(messageRow?.lastElementChild).toBe(bubble);
  expect(container.querySelector('[aria-label="Pending commands"]')).toBeNull();
});

it("submits once for two Enters before the cleared draft renders, then takes the next draft", async () => {
  const field = composer(await render());
  await type(field, "hello");
  await act(async () => {
    const enter = () => field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    enter();
    enter();
  });
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "hello" } }]);
  await type(field, "second");
  await press(field, {});
  expect(sentOperations()).toMatchObject([
    { case: "submitInput", value: { text: "hello" } },
    { case: "submitInput", value: { text: "second" } },
  ]);
});

it("sends the draft from the Send button, which an empty draft disables", async () => {
  const container = await render();
  expect(button(container, "Send").disabled).toBe(true);
  await type(composer(container), "hello");
  await act(async () => button(container, "Send").click());
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "hello" } }]);
});

it("resumes the existing Thread and restores sending on the open and reloaded pages", async () => {
  const ended = threadState({ rows: [viewState({ harness: "stopped", status: "ended" })] });
  const original = await render(ended);
  expect(composer(original).disabled).toBe(true);
  await act(async () => button(original, "Resume harness").click());
  expect(resumeThread).toHaveBeenCalledWith(THREAD.id);

  const running = threadState();
  await rerender(original, running);
  expect(composer(original).disabled).toBe(false);
  await type(composer(original), "from the open page");
  await press(composer(original), {});

  const reloaded = await render(running);
  expect(composer(reloaded).disabled).toBe(false);
  await type(composer(reloaded), "from the reloaded page");
  await press(composer(reloaded), {});
  expect(sentOperations()).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ case: "submitInput", value: expect.objectContaining({ text: "from the open page" }) }),
      expect.objectContaining({
        case: "submitInput",
        value: expect.objectContaining({ text: "from the reloaded page" }),
      }),
    ])
  );
  expect(new Set(vi.mocked(command).mock.calls.map(([, value]) => value.commandId)).size).toBe(2);
  expect(vi.mocked(command).mock.calls.every(([threadId]) => threadId === THREAD.id)).toBe(true);
});

it("shows the thread id in the More menu, not inline once a name is set", async () => {
  const container = await render();
  expect(document.body.textContent).not.toContain(THREAD.id);
  await act(async () => button(container, "More").click());
  expect(document.body.textContent).toContain(THREAD.id);
});

it("shuts the harness down from the More menu, not a control on the row", async () => {
  const container = await render();
  expect(document.body.textContent).not.toContain("Shut down harness");
  await act(async () => (await openMenuItem(container, "Shut down harness")).click());
  expect(sentOperations()).toMatchObject([{ case: "stopRunnerSession" }]);
});

it("disables shutdown while the harness is not running", async () => {
  const container = await render(threadState({ rows: [viewState({ harness: "stopped" })] }));
  expect((await openMenuItem(container, "Shut down harness")).disabled).toBe(true);
});

it.each([
  [{ feed_status: "failed" }, "Runner feed failed", "failed"],
  [{ feed_status: "ended" }, "Runner feed ended", "inactive"],
  [{ harness_state: "HARNESS_STATE_STOPPED" }, "Harness not running", "inactive"],
  [{ active_turn_id: "turn-1" }, "Turn running · Runner feed active · harness running", "running"],
  [{}, "Runner feed active · harness running", "idle"],
] as const)("uses shared thread row %o for the topbar status", async (row, label, kind) => {
  sharedThread = row;
  const container = await render();
  const dot = container.querySelector(".agentplane-thread-status-dot");
  expect(dot?.closest(".agentplane-composer-controls")).toBeNull();
  expect(mounted.at(-1)?.topbarTitle?.contains(dot ?? null)).toBe(true);
  expect(dot?.getAttribute("aria-label")).toBe(label);
  expect(dot?.getAttribute("data-status")).toBe(kind);
});

it("uses the shared list feed instead of a healthy conversation projection for the composer dot", async () => {
  sharedFeed = "failed";
  const failed = await render(threadState({ rows: [viewState()] }));
  expect(failed.querySelector('.agentplane-thread-status-dot[aria-label="Runner feed failed"]')).not.toBeNull();
});

const faviconSvg = (): string => decodeURIComponent(favicon.getAttribute("href")?.split(",")[1] ?? "");

it("shows green chevrons in the status dot and the favicon, and a leading glyph in the title, while a turn is active", async () => {
  vi.useFakeTimers();
  sharedThread = { active_turn_id: "turn-1" };
  const dot = (await render(threadState({ rows: [viewState({ activeTurn: "turn-1" })] }))).querySelector(
    ".agentplane-thread-status-dot"
  );
  expect(dot?.getAttribute("aria-label")).toBe("Turn running · Runner feed active · harness running");
  expect(dot?.getAttribute("data-status")).toBe("running");
  expect(dot?.querySelector("svg")).not.toBeNull();
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.running.glyph} Test thread · Running — Agentplane`);
  expect(favicon.getAttribute("href")).toMatch(/^data:image\/svg\+xml,/);
  const frame = faviconSvg();
  expect(frame).toContain('<path d="M5 14.5 27 5 18 27');
  expect(frame).toContain('fill="none"');
  expect(frame).not.toContain("<rect");
  expect(frame).not.toContain("<circle");
  expect(frame).toContain(`stroke="${THREAD_STATUS_MARKS.running.color}"`);
  // A background tab throttles timers, so the favicon changes on status and never on a clock.
  await act(async () => {
    vi.advanceTimersByTime(5_000);
  });
  expect(faviconSvg()).toBe(frame);
});

it("shows an idle thread as a dot in the idle color, in the favicon and with its own title glyph", async () => {
  sharedThread = {};
  const dot = (await render()).querySelector(".agentplane-thread-status-dot");
  expect(dot?.getAttribute("data-status")).toBe("idle");
  expect(dot?.querySelector("svg")).toBeNull();
  expect(faviconSvg()).toContain(`fill="${THREAD_STATUS_MARKS.idle.color}" stroke="#102a43"`);
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.idle.glyph} Test thread · Ready — Agentplane`);
});

it("does not show active-turn status when the runner is not active", async () => {
  sharedFeed = "ended";
  sharedThread = { active_turn_id: "turn-1" };
  const dot = (await render()).querySelector(".agentplane-thread-status-dot");
  expect(dot?.getAttribute("aria-label")).toBe("Runner feed ended");
  expect(dot?.getAttribute("data-status")).toBe("inactive");
});

// Retained history still says the harness runs and the feed failed; neither is live any more.
it.each([
  [{ archived: true }, [inventorySandbox()], "Thread archived"],
  [{ archived: false }, [], "Sandbox unavailable"],
  [{ archived: false }, [inventorySandbox("Suspended")], "Sandbox unavailable"],
])("shows an inactive dot for thread %o with sandboxes %o: %s", async (overrides, inventory, label) => {
  vi.mocked(getThread).mockResolvedValue({ ...THREAD, ...overrides });
  sharedThread = overrides;
  sandboxes = inventory;
  const dot = (await render(threadState({ rows: [viewState({ status: "failed" })] }))).querySelector(
    ".agentplane-thread-status-dot"
  );
  expect(dot?.getAttribute("aria-label")).toBe(label);
  expect(dot?.getAttribute("data-status")).toBe("inactive");
});

const RUNNING = inventorySandbox();
const SUSPENDED = inventorySandbox("Suspended");
const STALE = "sandboxes last updated 40 minutes ago";
const ABSENT = "Sandbox absent from last inventory snapshot. Current availability unknown";
const OUT_OF_DATE = /^What's on screen may be out of date; last update \d{2}:\d{2}:\d{2}$/;

function matching(text: string | RegExp): unknown {
  return typeof text === "string" ? expect.stringContaining(text) : expect.stringMatching(text);
}

// Each of these but the first disables the controls, which the composer's dot reports only as
// "Sandbox unavailable"; the header says why: the state the inventory last reported, or that the
// watch behind it has stalled or the stream been down a minute. A drop within the grace is a blip,
// on which the last inventory still stands.
it.each<[string, Inventory, { fresh?: boolean; droppedFor?: number }, string | null, string | RegExp | null]>([
  ["a running sandbox", [RUNNING], {}, null, null],
  ["an inventory not yet heard from", null, {}, null, null],
  ["a suspended sandbox", [SUSPENDED], {}, "Last observed Sandbox and Pod: Suspended.", null],
  ["a deleted sandbox", [], {}, "Sandbox no longer exists.", null],
  ["a running sandbox on a stale inventory", [RUNNING], { fresh: false }, null, STALE],
  ["an absence from a stale inventory", [], { fresh: false }, ABSENT, STALE],
  ["a running sandbox on a stream that just dropped", [RUNNING], { droppedFor: 0 }, null, null],
  ["an absence from a stream that just dropped", [], { droppedFor: 0 }, "Sandbox no longer exists.", null],
  ["an absence from a stream down past the grace", [], { droppedFor: DEGRADED_AFTER_MS }, ABSENT, null],
  ["a running sandbox on a stream down a minute", [RUNNING], { droppedFor: STALE_AFTER_MS }, null, OUT_OF_DATE],
  [
    "a suspended sandbox on a stream down a minute",
    [SUSPENDED],
    { droppedFor: STALE_AFTER_MS },
    "and Pod: Suspended.",
    OUT_OF_DATE,
  ],
])("explains %s in the header", async (_, inventory, { fresh = true, droppedFor }, status, alert) => {
  vi.useFakeTimers();
  sandboxes = inventory;
  inventoryFresh = fresh;
  inventoryDrops = droppedFor !== undefined;
  const container = await render();
  await act(async () => vi.advanceTimersByTime(droppedFor ?? 0));
  const texts = (role: string) => [...container.querySelectorAll(`[role="${role}"]`)].map((node) => node.textContent);
  expect(texts("status")).toEqual(status === null ? [] : [expect.stringContaining(status)]);
  expect(texts("alert")).toEqual(alert === null ? [] : [matching(alert)]);
});

it("shows the applied effort and sends a change command without optimistically changing it", async () => {
  const container = await render();
  const picker = container.querySelector<HTMLInputElement>('input[aria-label="Reasoning effort"]');
  expect(picker?.value).toBe("low");
  await act(async () => picker?.click());
  const high = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (option) => option.textContent === "high"
  );
  expect(high).toBeDefined();
  await act(async () => high?.click());
  expect(sentOperations()).toContainEqual({
    case: "changeReasoningEffort",
    value: expect.objectContaining({ effort: "high" }),
  });
  expect(picker?.value).toBe("low");
  await rerender(container, threadState({ rows: [viewState()] }));
  expect(picker?.value).toBe("low");
});

it.each([
  [{ caughtUp: false }, "Catching up…"],
  [{ windowError: "test shape gone" }, "Model unavailable"],
  [{ rows: [viewState({ status: "failed", model: null })] }, "Model unavailable"],
  [{ rows: [viewState({ model: null })] }, "Model"],
])("names why %o shows no model: %s", async (state, placeholder) => {
  const picker = (await render(threadState(state))).querySelector<HTMLInputElement>('input[aria-label="Model"]');
  expect(picker?.placeholder).toBe(placeholder);
});

// A stopped window says so in its own alert, and is not following the thread to be out of date.
it.each<[{ reconnectingFor: number; windowError?: string }, (string | RegExp)[]]>([
  [{ reconnectingFor: DEGRADED_AFTER_MS }, []],
  [{ reconnectingFor: STALE_AFTER_MS }, [OUT_OF_DATE]],
  [{ reconnectingFor: STALE_AFTER_MS, windowError: "test shape gone" }, ["Thread synchronization stopped"]],
])(
  "tells the reader the thread may be out of date only once its reads have failed a minute: %o",
  async (state, alerts) => {
    const container = await render(threadState(state));
    const shown = [...container.querySelectorAll('[role="alert"]')].map((node) => node.textContent);
    expect(shown).toEqual(alerts.map(matching));
  }
);

function message(commandId: string): Command {
  return create(CommandSchema, {
    commandId,
    operation: { case: "submitInput", value: { text: `Test input ${commandId}` } },
  });
}

function admission(value: Command): EventEntry {
  return create(EventEntrySchema, {
    cursor: 7n,
    origin: { sourceId: "test-runner", sequence: 7n },
    event: { observation: { case: "commandAdmitted", value: { command: value } } },
  });
}

async function admit(_threadId: string, value: Command): Promise<EventEntry> {
  return admission(value);
}

function sentIds(): string[] {
  return vi.mocked(command).mock.calls.map(([, value]) => value.commandId);
}

function pendingRow(container: HTMLDivElement, commandId: string): HTMLElement {
  const row = container.querySelector<HTMLElement>(`[data-command-id="${commandId}"]`);
  if (!row) throw new Error(`Missing pending command ${commandId}`);
  return row;
}

function retry(container: HTMLDivElement, commandId: string): HTMLButtonElement {
  const found = [...pendingRow(container, commandId).querySelectorAll("button")].find(
    (candidate) => candidate.textContent === "Retry"
  );
  if (!found) throw new Error(`Missing Retry for ${commandId}`);
  return found;
}

it("delivers a command an earlier page retained without the operator acting, and keeps its admission", async () => {
  new LocalCommands(THREAD.id).remember(message("retained-unadmitted"));
  vi.mocked(command).mockImplementation(admit);
  const container = await render();
  expect(sentIds()).toEqual(["retained-unadmitted"]);
  expect(pendingRow(container, "retained-unadmitted").textContent).toContain("Saved · awaiting effect");
  const [retained] = new LocalCommands(THREAD.id).getSnapshot().commands;
  expect(equals(EventEntrySchema, retained.admission!, admission(message("retained-unadmitted")))).toBe(true);
});

it("does not send a retained command whose admission it already holds", async () => {
  const store = new LocalCommands(THREAD.id);
  store.remember(message("retained-admitted"));
  store.acknowledge(message("retained-admitted"), admission(message("retained-admitted")));
  // Its unadmitted sibling being sent shows the mount's delivery ran.
  store.remember(message("retained-unadmitted"));
  vi.mocked(command).mockImplementation(admit);
  const container = await render();
  expect(sentIds()).toEqual(["retained-unadmitted"]);
  expect(pendingRow(container, "retained-admitted").textContent).toContain("Saved · awaiting effect");
});

it("shows a delivery that outlives its deadline as a failed attempt the operator can retry", async () => {
  new LocalCommands(THREAD.id).remember(message("hung"));
  let expire!: (reason: unknown) => void;
  vi.mocked(command)
    .mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        expire = reject;
      })
    )
    .mockImplementationOnce(admit);
  const container = await render();
  expect(pendingRow(container, "hung").textContent).toContain("Saved locally · awaiting admission");

  await act(async () => expire(new DOMException("signal timed out", "TimeoutError")));
  expect(pendingRow(container, "hung").textContent).toContain("signal timed out");
  expect(sentIds()).toEqual(["hung"]);

  await act(async () => retry(container, "hung").click());
  expect(sentIds()).toEqual(["hung", "hung"]);
  expect(pendingRow(container, "hung").textContent).toContain("Saved · awaiting effect");
  expect(pendingRow(container, "hung").textContent).not.toContain("signal timed out");
});

it("delivers a failed command again when the browser comes back online", async () => {
  new LocalCommands(THREAD.id).remember(message("offline"));
  vi.mocked(command)
    .mockRejectedValueOnce(new Error("the sandbox's runner is not answering"))
    .mockImplementationOnce(admit);
  const container = await render();
  expect(pendingRow(container, "offline").textContent).toContain("the sandbox's runner is not answering");
  expect(sentIds()).toEqual(["offline"]);

  await act(async () => window.dispatchEvent(new Event("online")));
  expect(sentIds()).toEqual(["offline", "offline"]);
  expect(pendingRow(container, "offline").textContent).toContain("Saved · awaiting effect");
});

function reference(ownerId: string, field: PayloadRef["field"]): PayloadRef {
  return {
    projection_epoch: "test-epoch",
    owner_cursor: "1",
    owner_id: ownerId,
    field,
    revision_cursor: "1",
    generation: "1",
    chunk_count: "1",
  };
}

function entity(
  entityKind: ThreadEntity["entityKind"],
  state: ThreadEntity["state"],
  refs: Partial<Pick<ThreadEntity, "textRef" | "argumentsRef" | "outputRef" | "inputRef">>
): ThreadEntity {
  return {
    threadId: "test-thread",
    projectionEpoch: "test-epoch",
    entityKind,
    entityId: "test-entity",
    entityIndex: "1",
    cursor: "1",
    revisionCursor: "1",
    pending: false,
    turnId: null,
    state,
    textRef: null,
    argumentsRef: null,
    outputRef: null,
    inputRef: null,
    ...refs,
  };
}

it("shows a server-only pending command as saved, not as a local delivery", async () => {
  const container = await render(
    threadState({
      rows: [
        viewState(),
        entity(
          "command",
          { operation: "change_model", outcome: "pending", outcome_cursor: null, outcome_reason: null },
          {}
        ),
      ],
    })
  );
  const pending = container.querySelector('[aria-label="Pending commands"]');
  expect(pending?.textContent).toContain("Saved · awaiting effect");
  expect(pending?.textContent).not.toContain("Saved locally");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent === "Retry")).toBe(false);
});

it("shows the failure and reason for a server-only command", async () => {
  const container = await render(
    threadState({
      rows: [
        viewState(),
        entity(
          "command",
          { operation: "change_model", outcome: "failed", outcome_cursor: "1", outcome_reason: "model unavailable" },
          {}
        ),
      ],
    })
  );
  const pending = container.querySelector('[aria-label="Pending commands"]');
  expect(pending?.textContent).toContain("Model change failed: model unavailable");
});

it.each([
  ["failed", "failed", "Input failed: runner unavailable"],
  ["noop", "noop", "Input not applied: harness was stopping"],
  ["effected", "confirmed", "Input applied"],
] as const)("shows a server-only %s input as a dismissible right-side message", async (outcome, phase, status) => {
  const container = await render(
    threadState({
      rows: [
        viewState(),
        entity(
          "command",
          {
            operation: "submit_input",
            outcome,
            outcome_cursor: "2",
            outcome_reason: outcome === "effected" ? null : status.split(": ")[1],
          },
          { inputRef: reference("failed-input", "command_input") }
        ),
      ],
    })
  );
  const bubble = container.querySelector<HTMLElement>(`.agentplane-user-bubble[data-message-phase="${phase}"]`);
  expect(bubble?.textContent).toContain(status);
  const messageRow = bubble?.parentElement;
  expect(messageRow?.firstElementChild?.textContent).toBe("Dismiss");
  expect(messageRow?.lastElementChild).toBe(bubble);
  await act(async () => (messageRow?.firstElementChild as HTMLButtonElement).click());
  expect(container.querySelector(`.agentplane-user-bubble[data-message-phase="${phase}"]`)).toBeNull();
  expect(new LocalCommands(THREAD.id).isDismissed("test-entity")).toBe(true);
});

it("replaces an applied input outcome with its confirmed message", async () => {
  const text = "The harness accepted this input";
  const confirmed = entity(
    "confirmed_input",
    { harness_message_id: "message", origin_command_ids: ["test-entity"] },
    { inputRef: reference("confirmed-input", "confirmed_input") }
  );
  const [historyRow] = await renderHistory([confirmed], false, { "confirmed-input:confirmed_input": text });
  expect(historyRow?.querySelector(".agentplane-user-bubble .agentplane-verbatim")?.textContent).toBe(text);

  const container = await render(
    threadState({
      rows: [
        viewState(),
        entity(
          "command",
          { operation: "submit_input", outcome: "effected", outcome_cursor: "1", outcome_reason: null },
          { inputRef: reference("command-input", "command_input") }
        ),
        confirmed,
      ],
    })
  );
  expect(container.querySelector('[aria-label="Input messages"]')).toBeNull();
});

it("keeps a still-pending sent message out of the pending-commands box, since it renders inline instead", async () => {
  const container = await render(
    threadState({
      rows: [
        viewState(),
        entity(
          "command",
          { operation: "submit_input", outcome: "pending", outcome_cursor: null, outcome_reason: null },
          { inputRef: reference("test-message", "command_input") }
        ),
      ],
    })
  );
  expect(container.querySelector('[aria-label="Pending commands"]')).toBeNull();
});

/** Serves every body at once; a card reads nothing else from the thread. */
function serving(bodies: ReadonlyMap<string, string>): ThreadSync {
  const unread = (): never => {
    throw new Error("an entity card reads only payloads");
  };
  return {
    Thread: unread,
    useThread: unread,
    useCommandRows: unread,
    usePayload: ({ owner_id, field }) => {
      const body = bodies.get(`${owner_id}:${field}`);
      if (body === undefined) throw new Error(`test fixture has no ${field} body for ${owner_id}`);
      return { body, error: null, retry: () => {} };
    },
  };
}

async function renderCard(card: ThreadEntity, bodies: Record<string, string>, live = false): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <ThreadSyncContext.Provider value={serving(new Map(Object.entries(bodies)))}>
          <RetainedDisclosureProvider>
            {/* The history's row carries the anchor; a card renders inside it. */}
            <div data-thread-anchor={card.cursor.toString()}>
              <EntityCard threadId="test-thread" entity={card} live={live} />
            </div>
          </RetainedDisclosureProvider>
        </ThreadSyncContext.Provider>
      </MantineProvider>
    )
  );
  return container;
}

type Observation = MessageInitShape<typeof EventSchema>["observation"];

function renderLifecycle(observation: string, event: Observation): Promise<HTMLDivElement> {
  const state = { observation, event: toJson(EventSchema, create(EventSchema, { observation: event })) };
  return renderCard(entity("lifecycle", state, {}), {});
}

async function disclose(container: HTMLElement, summary: string): Promise<HTMLDetailsElement> {
  const control = [...container.querySelectorAll("summary")].find((element) => element.textContent === summary);
  if (!control) throw new Error(`no ${summary} disclosure`);
  const details = control.parentElement as HTMLDetailsElement;
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  return details;
}

/** One element per history row, each holding what the thread view renders for it. */
async function renderHistory(
  segments: ThreadEntity[],
  live: boolean,
  bodies: Record<string, string> = {}
): Promise<HTMLElement[]> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <ThreadSyncContext.Provider value={serving(new Map(Object.entries(bodies)))}>
          <RetainedDisclosureProvider>
            {historyRows(segments).map((row) => (
              <section key={rowKey(row)}>
                <HistoryRowView threadId="test-thread" row={row} live={() => live} />
              </section>
            ))}
          </RetainedDisclosureProvider>
        </ThreadSyncContext.Provider>
      </MantineProvider>
    )
  );
  return [...container.querySelectorAll("section")];
}

function summaries(element: Element): (string | null)[] {
  return [...element.querySelectorAll("summary")].map((summary) => summary.textContent);
}

async function toggle(summary: Element): Promise<void> {
  const details = summary.parentElement as HTMLDetailsElement;
  await act(async () => {
    details.open = !details.open;
    details.dispatchEvent(new Event("toggle"));
  });
}

it("folds a run of tool calls and reasoning behind its summary until it is opened", async () => {
  const [run] = await renderHistory(
    [
      testItem(1, ItemKind.TOOL_CALL, { tool_name: "test-read", completion: "tool", tool_succeeded: true }),
      testItem(2, ItemKind.REASONING, {}, { textRef: reference("test-entity-2", "text") }),
      testItem(3, ItemKind.TOOL_CALL, { tool_name: "test-shell", completion: "tool", tool_succeeded: false }),
    ],
    false,
    { "test-entity-2:text": "The plan is to **inspect** the evidence." }
  );
  const summary = run.querySelector("summary")!;
  expect(summary.textContent).toContain("2 tool calls, 1 reasoning step");
  expect(summary.querySelector('[role="img"][aria-label="Failed"]')).not.toBeNull();
  expect(run.textContent).not.toContain("test-read");

  await toggle(summary);
  expect(run.textContent).toContain("test-read");
  expect(run.textContent).toContain("test-shell");
  expect(run.textContent).toContain("The plan is to inspect the evidence.");
  expect(run.querySelector(".agentplane-step-preview strong")?.textContent).toBe("inspect");
  // Each step keeps its own evidence; the run is not an entity and has none.
  expect(run.querySelectorAll('button[aria-label="Evidence"]')).toHaveLength(3);
});

it("marks an unfinished run as streaming in the live turn, and as incomplete once that is over", async () => {
  const segments = [testItem(1, ItemKind.REASONING, { completion: null }), testItem(2, ItemKind.TOOL_CALL)];
  const [live] = await renderHistory(segments, true);
  expect(live.querySelector('summary [role="img"]')?.getAttribute("aria-label")).toBe("Streaming");
  const [retained] = await renderHistory(segments, false);
  expect(retained.querySelector('summary [role="img"]')?.getAttribute("aria-label")).toBe("Incomplete");
});

it("puts a live assistant-text cursor inline after its Markdown body", async () => {
  const body = "The answer is still being written.";
  const [row] = await renderHistory(
    [testItem(1, ItemKind.ASSISTANT_TEXT, { completion: null }, { textRef: reference("streaming-reply", "text") })],
    true,
    { "streaming-reply:text": body }
  );
  const cursor = row.querySelector<HTMLElement>(".agentplane-streaming-cursor");

  expect(cursor?.getAttribute("aria-label")).toBe("Streaming");
  expect(cursor?.getAttribute("data-character")).toBe(STREAMING_CURSOR);
  expect(cursor?.closest(".agentplane-markdown")).not.toBeNull();
  expect(cursor?.parentElement?.textContent?.trimEnd()).toBe(body);
  expect(row.querySelector(".mantine-Badge-root")).toBeNull();
});

it("shows a lone reasoning step as its own reasoning block, and assistant text without a role label", async () => {
  const [reasoning, answer] = await renderHistory(
    [
      testItem(1, ItemKind.REASONING, {}, { textRef: reference("test-entity-1", "text") }),
      testItem(2, ItemKind.ASSISTANT_TEXT, {}, { textRef: reference("test-entity-2", "text") }),
    ],
    false,
    {
      "test-entity-1:text": "Reasoning preview body",
      "test-entity-2:text": "Test body of test-entity-2",
    }
  );
  expect(reasoning.querySelector("details.agentplane-step-details")).toBeNull();
  expect(reasoning.querySelector(".agentplane-step-title")?.textContent).toBe("Reasoning");
  expect(reasoning.textContent).toContain("Reasoning preview body");
  expect(answer.textContent).toContain("Test body of test-entity-2");
  for (const row of [reasoning, answer]) expect(row.textContent).not.toMatch(/assistant/i);
});

const PROSE = "Run **every** test\n- first";

describe("recovery presentation", () => {
  it.each([RecoveryDisposition.RETAINED, RecoveryDisposition.REVISED, RecoveryDisposition.UNKNOWN])(
    "does not revive an interrupted reply's streaming cursor for recovery %s",
    async (recovery) => {
      const [row] = await renderHistory(
        [
          testItem(
            1,
            ItemKind.ASSISTANT_TEXT,
            { completion: null, recovery },
            { textRef: reference("recovered-reply", "text") }
          ),
        ],
        true,
        { "recovered-reply:text": "Interrupted reply" }
      );
      expect(row.querySelector(".agentplane-streaming-cursor")).toBeNull();
      expect(row.querySelector('[aria-label="Interrupted"]')).not.toBeNull();
    }
  );

  it.each([null, "text"])("labels retained text with completion %s", async (completion) => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.ASSISTANT_TEXT,
        { completion, recovery: RecoveryDisposition.RETAINED },
        { textRef: reference("test-entity-1", "text") }
      ),
      { "test-entity-1:text": "Remember the name in the margin" }
    );
    expect(container.textContent).toContain("Remember the name in the margin");
    expect(container.querySelector('[aria-label="Retained in context"]')).not.toBeNull();
  });

  it("collapses discarded text and preserves it behind a disclosure", async () => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.ASSISTANT_TEXT,
        { completion: null, recovery: RecoveryDisposition.ABSENT },
        { textRef: reference("test-entity-1", "text") }
      ),
      { "test-entity-1:text": "The seam widened." }
    );
    expect(container.textContent).not.toContain("The seam widened.");
    await disclose(container, "Discarded output (not retained in model context)");
    expect(container.textContent).toContain("The seam widened.");
    expect(container.querySelector('[aria-label="Not retained in context"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Interrupted"]')).not.toBeNull();
  });

  it("shows unknown retention and its reason without hiding the observed text", async () => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.ASSISTANT_TEXT,
        {
          completion: null,
          recovery: RecoveryDisposition.UNKNOWN,
          recovery_reason: "The harness history could not be inspected.",
        },
        { textRef: reference("test-entity-1", "text") }
      ),
      { "test-entity-1:text": "The bells were ringing." }
    );
    expect(container.textContent).toContain("The bells were ringing.");
    expect(container.textContent).toContain("The harness history could not be inspected.");
    expect(container.querySelector('[aria-label="Retention unknown"]')).not.toBeNull();
  });

  it("labels revised content as continuation, without inventing a tool outcome", async () => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.TOOL_CALL,
        {
          tool_name: "Bash",
          completion: null,
          tool_succeeded: null,
          recovery: RecoveryDisposition.REVISED,
        },
        { outputRef: reference("test-entity-1", "output") }
      ),
      { "test-entity-1:output": "aborted" }
    );
    expect(container.querySelector('[aria-label="Revised for continuation"]')).not.toBeNull();
    await toggle(container.querySelector("summary")!);
    expect(container.textContent).toContain("Recovery content does not establish a tool execution outcome.");
    expect(container.textContent).toContain("Continuation output");
    expect(container.textContent).toContain("aborted");
    expect(container.querySelector('[aria-label="Succeeded"]')).toBeNull();
    expect(container.querySelector('[aria-label="Failed"]')).toBeNull();
  });

  it("preserves a successful execution result when its context was discarded", async () => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.TOOL_CALL,
        {
          tool_name: "Bash",
          completion: "tool",
          tool_succeeded: true,
          recovery: RecoveryDisposition.ABSENT,
        },
        { outputRef: reference("test-entity-1", "output") }
      ),
      { "test-entity-1:output": "Created report.txt" }
    );
    await disclose(container, "Bash: discarded context (not retained in model context)");
    expect(container.textContent).toContain("does not undo tool side effects");
    expect(container.querySelector('[aria-label="Succeeded"]')).not.toBeNull();
    expect(container.textContent).toContain("Created report.txt");
  });

  it("summarizes recovery in a collapsed tool run without marking recovered items streaming", async () => {
    const [run] = await renderHistory(
      [
        testItem(1, ItemKind.TOOL_CALL, { completion: null, recovery: RecoveryDisposition.UNKNOWN }),
        testItem(2, ItemKind.TOOL_CALL, {
          completion: "tool",
          tool_succeeded: false,
          recovery: RecoveryDisposition.RETAINED,
        }),
      ],
      true
    );
    expect(run.querySelector('[aria-label="Retention unknown"]')).not.toBeNull();
    expect(run.querySelector('[aria-label="Retained in context"]')).not.toBeNull();
    expect(run.querySelector('[aria-label="Interrupted"]')).not.toBeNull();
    expect(run.querySelector('[aria-label="Failed"]')).not.toBeNull();
    expect(run.querySelector('[aria-label="Streaming"]')).toBeNull();
  });
});

function toolEntity(toolName: string): ThreadEntity {
  return entity(
    "item",
    {
      kind: ItemKind.TOOL_CALL,
      tool_name: toolName,
      completion: "tool",
      tool_succeeded: true,
      recovery: null,
      recovery_reason: "",
    },
    { argumentsRef: reference("test-tool", "arguments"), outputRef: reference("test-tool", "output") }
  );
}

/** A completed tool call whose arguments and output are the given values. */
function renderTool(toolName: string, args: unknown, output = "test-output"): Promise<HTMLDivElement> {
  return renderCard(toolEntity(toolName), {
    "test-tool:arguments": JSON.stringify(args),
    "test-tool:output": output,
  });
}

const lineOf = (container: HTMLElement): string | undefined =>
  container.querySelector(".agentplane-step-preview")?.textContent ?? undefined;

describe("reasoning step marks", () => {
  const reasoningStep = (state: Partial<Extract<ThreadEntity["state"], { kind: number }>>): ThreadEntity =>
    entity(
      "item",
      {
        kind: ItemKind.REASONING,
        tool_name: "",
        completion: null,
        tool_succeeded: null,
        recovery: null,
        recovery_reason: "",
        ...state,
      },
      { textRef: reference("test-reasoning", "text") }
    );
  const bodies = { "test-reasoning:text": "Weighing the next step." };

  it.each([
    [true, "Streaming", true],
    [false, "Incomplete", false],
  ])(
    "marks an unfinished step (live: %s) by its title, with no badge row above the line",
    async (live, label, breathes) => {
      const container = await renderCard(reasoningStep({}), bodies, live);
      const title = container.querySelector(".agentplane-step-title")!;
      expect(title.textContent).toBe("Reasoning");
      expect(title.getAttribute("style")).toContain("blue");
      expect(title.getAttribute("title")).toBe(label);
      expect(title.classList.contains("agentplane-step-title--streaming")).toBe(breathes);
      expect(container.querySelector(`[aria-label="${label}"]`)).toBeNull();
    }
  );

  it("leaves a finished step in the dimmed title", async () => {
    const container = await renderCard(reasoningStep({ completion: "text" }), bodies);
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("style")).not.toContain("blue");
  });

  it("leaves an interrupted step to its own badges, which the title has no mark for", async () => {
    const container = await renderCard(reasoningStep({ recovery: RecoveryDisposition.RETAINED }), bodies);
    expect(container.querySelector('[aria-label="Interrupted"]')).not.toBeNull();
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("style")).not.toContain("blue");
  });
});

describe("tool call rows", () => {
  const COMMAND = "docker ps --all\n  --format '{{.Names}}'";

  it("folds a Claude Bash call to the line the model wrote for it, and draws nothing else", async () => {
    const container = await renderTool("Bash", { command: COMMAND, description: "List every container" });
    expect(container.querySelector(".agentplane-step-title")?.textContent).toBe("Bash");
    expect(lineOf(container)).toBe("List every container");
    expect(container.querySelector(".agentplane-code-block")).toBeNull();
    expect(container.textContent).not.toContain("test-output");
  });

  it("folds a Bash call with no description to its command on one line, as highlighted shell", async () => {
    const container = await renderTool("Bash", { command: `set -e\n${COMMAND}` });
    expect(lineOf(container)).toBe("set -e docker ps --all --format '{{.Names}}'");
    expect(
      container.querySelector(".agentplane-step-preview .agentplane-code-inline .agentplane-tok-keyword")?.textContent
    ).toBe("set");
  });

  it("does not highlight what the model wrote to say what a command is for", async () => {
    const container = await renderTool("Bash", { command: COMMAND, description: "List every container" });
    expect(container.querySelector(".agentplane-code-inline")).toBeNull();
  });

  it("folds Codex's command to the script it ran, without the shell that ran it", async () => {
    const container = await renderTool("commandExecution", {
      command: String.raw`/bin/bash -lc "echo \"test-script\""`,
      cwd: "/test-workspace",
    });
    expect(container.querySelector(".agentplane-step-title")?.textContent).toBe("Shell");
    expect(lineOf(container)).toBe('echo "test-script"');
  });

  it("opens to the command as shell and the output, with the model's reason and how it ran", async () => {
    const container = await renderTool(
      "Bash",
      { command: COMMAND, description: "List every container", timeout: 5000 },
      "test-container\n"
    );
    await toggle(container.querySelector("summary")!);
    expect(container.textContent).toContain("List every container");
    expect(container.textContent).toContain("Timeout 5000 ms");
    const [command, output] = [...container.querySelectorAll(".agentplane-code-block")].map((block) =>
      [...block.querySelectorAll(".cm-line")].map((line) => line.textContent ?? "").join("\n")
    );
    expect(command).toBe(COMMAND);
    expect(output).toBe("test-container");
    expect(container.textContent).not.toContain('"command"');
  });

  it("shows the JSON a command call holds in place of the command while Raw is on", async () => {
    const container = await renderTool("Bash", { command: "ls", description: "List files" });
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    await toggle(container.querySelector("summary")!);
    // On the title line, outside its summary and apart from the content below it.
    const raw = container.querySelector<HTMLInputElement>(".agentplane-step-row > .agentplane-step-controls input")!;
    expect(raw.type).toBe("checkbox");
    const firstBlock = () => container.querySelector(".agentplane-code-block")?.textContent;
    expect(firstBlock()).toBe("ls");

    await act(async () => raw.click());
    expect(firstBlock()).toContain('"command":"ls"');
    expect(firstBlock()).toContain('"description":"List files"');

    await act(async () => raw.click());
    expect(firstBlock()).toBe("ls");
  });

  it("folds any other tool to its arguments' JSON, which is all it can open to", async () => {
    const container = await renderTool("Read", { file_path: "test-file" });
    expect(container.querySelector(".agentplane-step-title")?.textContent).toBe("Read");
    expect(lineOf(container)).toBe('{"file_path":"test-file"}');
    await toggle(container.querySelector("summary")!);
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it("draws a Bash call it could not show whole as its JSON, with nothing to switch", async () => {
    const container = await renderTool("Bash", { command: "ls", test_extra: true });
    await toggle(container.querySelector("summary")!);
    expect(container.querySelector(".agentplane-code-block")?.textContent).toContain('"test_extra":true');
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it("marks a failed call by its title in red while folded, and by a badge once opened", async () => {
    const container = await renderCard(
      entity(
        "item",
        {
          kind: ItemKind.TOOL_CALL,
          tool_name: "Bash",
          completion: "tool",
          tool_succeeded: false,
          recovery: null,
          recovery_reason: "",
        },
        { outputRef: reference("test-tool", "output") }
      ),
      { "test-tool:output": "test-failure" }
    );
    const title = container.querySelector(".agentplane-step-title")!;
    expect(title.getAttribute("style")).toContain("red");
    expect(title.getAttribute("title")).toBe("Failed");
    expect(container.querySelector('[aria-label="Failed"]')).toBeNull();

    await toggle(container.querySelector("summary")!);
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("style")).not.toContain("red");
    expect(container.querySelector('[aria-label="Failed"]')).not.toBeNull();
  });

  it.each([
    [true, "Streaming", true],
    [false, "Incomplete", false],
  ])(
    "marks a call still unfinished (live: %s) by a blue title while folded, and by a badge once opened",
    async (live, label, breathes) => {
      const container = await renderCard(
        entity(
          "item",
          {
            kind: ItemKind.TOOL_CALL,
            tool_name: "Bash",
            completion: null,
            tool_succeeded: null,
            recovery: null,
            recovery_reason: "",
          },
          { outputRef: reference("test-tool", "output") }
        ),
        { "test-tool:output": "test-partial" },
        live
      );
      const title = () => container.querySelector(".agentplane-step-title")!;
      expect(title().getAttribute("style")).toContain("blue");
      expect(title().getAttribute("title")).toBe(label);
      // Only a call something is working on breathes.
      expect(title().classList.contains("agentplane-step-title--streaming")).toBe(breathes);
      expect(container.querySelector(`[aria-label="${label}"]`)).toBeNull();

      await toggle(container.querySelector("summary")!);
      expect(title().getAttribute("style")).not.toContain("blue");
      expect(title().classList.contains("agentplane-step-title--streaming")).toBe(false);
      expect(container.querySelector(`[aria-label="${label}"]`)).not.toBeNull();
    }
  );

  it("leaves a call that went well in the dimmed title every line has", async () => {
    const container = await renderTool("Bash", { command: "ls" });
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("style")).not.toContain("red");
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("title")).toBeNull();
  });

  it("keeps a call's open state and Raw where the reader left them when its row leaves the DOM", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    mounted.push({ root, container });
    const bodies = new Map([
      ["test-tool:arguments", JSON.stringify({ command: "ls" })],
      ["test-tool:output", "test-output"],
    ]);
    // One provider throughout, as the thread has: only the row mounts and unmounts.
    const show = (visible: boolean) =>
      act(async () =>
        root.render(
          <MantineProvider env="test">
            <ThreadSyncContext.Provider value={serving(bodies)}>
              <RetainedDisclosureProvider>
                {visible && <EntityCard threadId="test-thread" entity={toolEntity("Bash")} live={false} />}
              </RetainedDisclosureProvider>
            </ThreadSyncContext.Provider>
          </MantineProvider>
        )
      );
    await show(true);
    await toggle(container.querySelector("summary")!);
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());

    await show(false);
    expect(container.querySelector("details")).toBeNull();
    await show(true);
    expect(container.querySelector("details")?.open).toBe(true);
    expect(container.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true);
  });
});

describe("EntityCard", () => {
  it("renders a tool call's JSON arguments highlighted and its plain output verbatim, both as code", async () => {
    const container = await renderTool("Read", { file_path: "test-file", limit: 30 }, PROSE);
    await toggle(container.querySelector("summary")!);

    const [args, output] = container.querySelectorAll(".agentplane-code-block");
    expect(args.querySelector(".cm-editor")).not.toBeNull();
    expect(args.textContent).toContain('"file_path":"test-file"');
    expect(args.textContent).toContain('"limit":30');
    expect([...output.querySelectorAll(".cm-line")].map((line) => line.textContent ?? "").join("\n")).toBe(PROSE);
    expect(output.querySelector("strong, li")).toBeNull();
  });

  it("renders the assistant's text as Markdown", async () => {
    const container = await renderCard(
      entity(
        "item",
        {
          kind: ItemKind.ASSISTANT_TEXT,
          tool_name: "",
          completion: PROSE,
          tool_succeeded: null,
          recovery: null,
          recovery_reason: "",
        },
        { textRef: reference("test-reply", "text") }
      ),
      { "test-reply:text": PROSE }
    );
    expect(container.querySelector(".agentplane-markdown strong")?.textContent).toBe("every");
    expect(container.querySelector(".agentplane-markdown li")?.textContent).toBe("first");
  });

  it("renders the operator's input as typed, not as Markdown", async () => {
    const container = await renderCard(
      entity(
        "confirmed_input",
        { harness_message_id: "test-message", origin_command_ids: [] },
        { inputRef: reference("test-message", "confirmed_input") }
      ),
      { "test-message:confirmed_input": PROSE }
    );
    expect(container.querySelector(".agentplane-user-bubble .agentplane-verbatim")?.textContent).toBe(PROSE);
    expect(container.querySelector(".agentplane-markdown, strong, li")).toBeNull();
  });

  it("renders a still-pending sent message as the same bubble, marked pending", async () => {
    const container = await renderCard(
      entity(
        "command",
        { operation: "submit_input", outcome: "pending", outcome_cursor: null, outcome_reason: null },
        { inputRef: reference("test-message", "command_input") }
      ),
      { "test-message:command_input": PROSE }
    );
    const bubble = container.querySelector<HTMLElement>(".agentplane-user-bubble");
    expect(bubble?.querySelector(".agentplane-verbatim")?.textContent).toBe(PROSE);
    expect(bubble?.style.fontStyle).toBe("italic");
    expect(bubble?.textContent).toContain("Saved · awaiting effect");
  });

  it.each<[string, Observation, string]>([
    [
      "turn_completed",
      { case: "turnCompleted", value: { turnId: "test-turn", status: TurnStatus.COMPLETED } },
      "Turn completed",
    ],
    [
      "turn_completed",
      { case: "turnCompleted", value: { turnId: "test-turn", status: TurnStatus.INTERRUPTED } },
      "Turn interrupted",
    ],
    ["turn_started", { case: "turnStarted", value: { turnId: "test-turn", model: "test-model" } }, "Turn started"],
    [
      "model_changed",
      { case: "modelChanged", value: { previousModel: "test-model", model: "test-model-next" } },
      "Model changed to test-model-next",
    ],
    [
      "reasoning_effort_changed",
      { case: "reasoningEffortChanged", value: { previousEffort: "low", effort: "high" } },
      "Reasoning effort changed to high",
    ],
    ["harness_exited", { case: "harnessExited", value: { exitCode: 3 } }, "Harness exited with code 3"],
  ])("shows an ordinary %s as one line with no disclosure of its own", async (observation, event, line) => {
    const container = await renderLifecycle(observation, event);
    const row = container.querySelector('[data-thread-anchor="1"]')!;
    // No disclosure of its own: the raw event is reached through the row's Evidence.
    expect(row.querySelector("details, summary")).toBeNull();
    expect(row.querySelector('button[aria-label="Evidence"]')).not.toBeNull();
    expect(row.textContent).toBe(line);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it.each(['Test API failure: HTTP 429\n<img src="x" onerror="throw new Error()">', ""])(
    "shows a failed turn's error as a plain-text alert: %j",
    async (error) => {
      const container = await renderLifecycle("turn_completed", {
        case: "turnCompleted",
        value: { turnId: "test-failed-turn", status: TurnStatus.FAILED, error },
      });
      const alert = container.querySelector('[data-thread-anchor="1"] [role="alert"]')!;
      expect(alert.textContent).toBe(`Turn failed${error || "The harness reported no error details."}`);
      expect(alert.querySelector("img")).toBeNull();
    }
  );

  it("shows an interrupted turn's error dimmed beneath its line, not as an alert", async () => {
    const container = await renderLifecycle("turn_completed", {
      case: "turnCompleted",
      value: { turnId: "test-turn", status: TurnStatus.INTERRUPTED, error: "test interrupt detail" },
    });
    expect(container.querySelector('[data-thread-anchor="1"]')?.textContent).toBe(
      "Turn interruptedtest interrupt detail"
    );
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it.each<[string, string, Observation]>([
    [
      "Turn losttest harness exited during the turn",
      "turn_completed",
      {
        case: "turnCompleted",
        value: { turnId: "test-turn", status: TurnStatus.PROCESS_LOST, error: "test harness exited during the turn" },
      },
    ],
    [
      "Turn ended without a status",
      "turn_completed",
      { case: "turnCompleted", value: { turnId: "test-turn", status: TurnStatus.UNSPECIFIED } },
    ],
    ["Harness lost", "harness_lost", { case: "harnessLost", value: {} }],
  ])("keeps an abnormal ending prominent: %s", async (text, observation, event) => {
    const container = await renderLifecycle(observation, event);
    expect(container.querySelector('[data-thread-anchor="1"] [role="alert"]')?.textContent).toBe(text);
  });
});
