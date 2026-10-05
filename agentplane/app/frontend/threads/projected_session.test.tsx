// @vitest-environment happy-dom

import { create } from "@bufbuild/protobuf";
import { TEST_REASONING_EFFORTS } from "../test_model_catalog";
import { MantineProvider } from "@mantine/core";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../../protocol/event_log_pb";
import { ItemKind, RecoveryDisposition } from "../../../protocol/event_pb";
import type * as ClientModule from "../client";
import { command, getThread, models, resumeThread, type SandboxView, type ThreadView } from "../client";
import { historyRows, rowKey } from "./history_rows";
import { SandboxesLiveProvider, ThreadsLiveProvider, useRequiredThreadsLive } from "../live";
import { LocalCommands } from "./local_commands";
import { STREAMING_CURSOR } from "../markdown";
import { HistoryRowView, ProjectedSession } from "./projected_session";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import { DEGRADED_AFTER_MS, STALE_AFTER_MS } from "../stream_status";
import { THREAD_STATUS_MARKS } from "../status_mark";
import { testItem } from "./thread_entity_fixture";
import {
  badgeLabels,
  entity,
  reference,
  serving,
  THREAD,
  threadState,
  toggle,
  viewState,
} from "./thread_state_fixture";
import { ThreadSyncContext, type ThreadEntity, type ThreadState, type ThreadSync } from "./thread_sync";
import { TopbarContext } from "../topbar";

vi.mock("../client", async (importOriginal) => ({
  ...(await importOriginal<typeof ClientModule>()),
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
let eventSourceUrls: string[] = [];
let threadRows: ThreadView[] | null = null;

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
  eventSourceUrls = [];
  threadRows = null;
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
        eventSourceUrls.push(url);
        queueMicrotask(() => {
          if (sandboxes === null) return;
          this.dispatchEvent(
            new MessageEvent("snapshot", {
              data: JSON.stringify({
                sandboxes,
                ...(url === "/live/threads"
                  ? {
                      threads: threadRows ?? [
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
    root.render(<TestLiveProviders>{content}</TestLiveProviders>);
  });
  container.append(topbarTitle, topbarActions);
  return container;
}

function TestLiveProviders({ children }: { children: JSX.Element }): JSX.Element {
  return (
    <ThreadsLiveProvider>
      <SandboxesLiveProvider>{children}</SandboxesLiveProvider>
    </ThreadsLiveProvider>
  );
}

function WaitForThreadsSnapshot({ children }: { children: JSX.Element }): JSX.Element | null {
  const live = useRequiredThreadsLive();
  return live.snapshot ? children : null;
}

async function renderAfterThreadsSnapshot(state: ThreadState = threadState()): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const topbarTitle = document.createElement("div");
  const topbarActions = document.createElement("div");
  const root = createRoot(container);
  mounted.push({ root, container, topbarTitle, topbarActions });
  await act(async () => {
    const content = page(state, topbarTitle, topbarActions);
    root.render(
      <TestLiveProviders>
        <WaitForThreadsSnapshot>{content}</WaitForThreadsSnapshot>
      </TestLiveProviders>
    );
  });
  container.append(topbarTitle, topbarActions);
  return container;
}

function page(
  state: ThreadState,
  topbarTitle: HTMLDivElement,
  topbarActions: HTMLDivElement,
  mountKey?: string
): JSX.Element {
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
          <ProjectedSession key={mountKey} threadId={THREAD.id} />
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
    current.root.render(<TestLiveProviders>{page(state, topbarTitle, topbarActions)}</TestLiveProviders>)
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

function buttonIn(row: Element | null | undefined, text: string): HTMLButtonElement | undefined {
  return [...(row?.querySelectorAll("button") ?? [])].find((candidate) => candidate.textContent === text);
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
  expect(bubble?.parentElement?.querySelector('[role="status"]')?.textContent).toBe(
    "Saved locally · awaiting admission"
  );
  expect(bubble?.querySelector('[role="status"]')).toBeNull();
  expect(buttonIn(bubble?.parentElement, "Retry")).toBeDefined();
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
  // Where a harness shutdown settles: its feed has ended and the harness is down.
  [{ feed_status: "ended", harness_state: "HARNESS_STATE_STOPPED" }, "Harness not running", "stopped"],
  [{ harness_state: "HARNESS_STATE_STOPPED" }, "Harness not running", "stopped"],
  [{ active_turn_id: "turn-1" }, "Turn running · Runner feed active · harness running", "running"],
  [{}, "Runner feed active · harness running", "idle"],
  [{ last_turn_status: "TURN_STATUS_FAILED" }, "Turn failed · Runner feed active · harness running", "turn_error"],
  [{ last_turn_status: "TURN_STATUS_PROCESS_LOST" }, "Turn lost · Runner feed active · harness running", "turn_error"],
] as const)("uses shared thread row %o for the topbar status", async (row, label, kind) => {
  sharedThread = row;
  const container = await render();
  const indicator = container.querySelector(".agentplane-thread-status-indicator");
  expect(mounted.at(-1)?.topbarTitle?.contains(indicator ?? null)).toBe(true);
  expect(indicator?.getAttribute("aria-label")).toBe(label);
  expect(indicator?.getAttribute("data-status")).toBe(kind);
});

const faviconSvg = (): string => decodeURIComponent(favicon.getAttribute("href")?.split(",")[1] ?? "");

it("shows green chevrons in the status indicator and the favicon, and a leading glyph in the title, while a turn is active", async () => {
  vi.useFakeTimers();
  sharedThread = { active_turn_id: "turn-1" };
  const indicator = (await render(threadState({ rows: [viewState({ activeTurn: "turn-1" })] }))).querySelector(
    ".agentplane-thread-status-indicator"
  );
  expect(indicator?.getAttribute("aria-label")).toBe("Turn running · Runner feed active · harness running");
  expect(indicator?.getAttribute("data-status")).toBe("running");
  expect(indicator?.querySelector("svg")).not.toBeNull();
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
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("data-status")).toBe("idle");
  expect(indicator?.querySelector("svg")).toBeNull();
  expect(faviconSvg()).toContain(`fill="${THREAD_STATUS_MARKS.idle.color}" stroke="#102a43"`);
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.idle.glyph} Test thread · Ready — Agentplane`);
});

it("shows an idle thread whose last turn failed as a red warning icon, in the favicon and with its own title glyph", async () => {
  sharedThread = { last_turn_status: "TURN_STATUS_FAILED" };
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("data-status")).toBe("turn_error");
  expect(indicator?.querySelector("svg")).not.toBeNull();
  expect(faviconSvg()).toContain(`fill="${THREAD_STATUS_MARKS.turn_error.color}"`);
  // Not the red dot of a failed runner feed, which has a white inner ring.
  expect(faviconSvg()).not.toContain('stroke="#fff"');
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.turn_error.glyph} Test thread · Turn failed — Agentplane`);
});

it("shows a stopped harness as an icon in the status indicator and the favicon, with its own title glyph", async () => {
  sharedThread = { harness_state: "HARNESS_STATE_STOPPED" };
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("data-status")).toBe("stopped");
  expect(indicator?.querySelector("svg")).not.toBeNull();
  expect(faviconSvg()).toContain(`stroke="${THREAD_STATUS_MARKS.stopped.color}"`);
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.stopped.glyph} Test thread · Stopped — Agentplane`);
});

it("does not show active-turn status when the runner is not active", async () => {
  sharedFeed = "ended";
  sharedThread = { active_turn_id: "turn-1" };
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("aria-label")).toBe("Runner feed ended");
  expect(indicator?.getAttribute("data-status")).toBe("inactive");
});

// Retained history still says the harness runs and the feed failed; an archived thread shows the archive
// icon instead, in the status indicator, the favicon and the title glyph.
it("shows an archived thread as the archive icon, in the favicon and with its own title glyph", async () => {
  vi.mocked(getThread).mockResolvedValue({ ...THREAD, archived: true });
  sharedThread = { archived: true };
  sandboxes = [inventorySandbox()];
  const indicator = (await render(threadState({ rows: [viewState({ status: "failed" })] }))).querySelector(
    ".agentplane-thread-status-indicator"
  );
  expect(indicator?.getAttribute("aria-label")).toBe("Thread archived");
  expect(indicator?.getAttribute("data-status")).toBe("archived");
  expect(indicator?.querySelector("svg")).not.toBeNull();
  expect(faviconSvg()).toContain(`stroke="${THREAD_STATUS_MARKS.archived.color}"`);
  expect(faviconSvg()).not.toContain("<circle");
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.archived.glyph} Test thread · Archived — Agentplane`);
});

it.each([
  [{ archived: false }, [], "Sandbox unavailable"],
  [{ archived: false }, [inventorySandbox("Suspended")], "Sandbox unavailable"],
])("shows an inactive dot for thread %o with sandboxes %o: %s", async (overrides, inventory, label) => {
  vi.mocked(getThread).mockResolvedValue({ ...THREAD, ...overrides });
  sharedThread = overrides;
  sandboxes = inventory;
  const indicator = (await render(threadState({ rows: [viewState({ status: "failed" })] }))).querySelector(
    ".agentplane-thread-status-indicator"
  );
  expect(indicator?.getAttribute("aria-label")).toBe(label);
  expect(indicator?.getAttribute("data-status")).toBe("inactive");
});

const RUNNING = inventorySandbox();
const SUSPENDED = inventorySandbox("Suspended");
const STALE = "sandboxes last updated 40 minutes ago";
const ABSENT = "Sandbox absent from last inventory snapshot. Current availability unknown";
const OUT_OF_DATE = /^What's on screen may be out of date; last update \d{2}:\d{2}:\d{2}$/;

function matching(text: string | RegExp): unknown {
  return typeof text === "string" ? expect.stringContaining(text) : expect.stringMatching(text);
}

it("keeps one sandbox inventory stream mounted across thread route remounts", async () => {
  const container = await render();
  const current = mounted.find((entry) => entry.container === container);
  const topbarTitle = current?.topbarTitle;
  const topbarActions = current?.topbarActions;
  if (!current || !topbarTitle || !topbarActions) throw new Error("Missing mounted thread page");

  await act(async () => {
    current.root.render(
      <TestLiveProviders>{page(threadState(), topbarTitle, topbarActions, "next-thread")}</TestLiveProviders>
    );
  });

  expect(eventSourceUrls.filter((url) => url === "/live/sandboxes")).toHaveLength(1);
});

it("mounts the thread from the live snapshot without fetching its metadata again", async () => {
  const container = await renderAfterThreadsSnapshot();

  expect(getThread).not.toHaveBeenCalled();
  expect(container.querySelector("textarea")).not.toBeNull();
});

it("fetches thread metadata when it is absent from the live snapshot", async () => {
  threadRows = [];

  const container = await render();

  expect(getThread).toHaveBeenCalledExactlyOnceWith(THREAD.id);
  expect(container.querySelector("textarea")).not.toBeNull();
});

// Each of these but the first disables the controls, which the composer's indicator reports only as
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
  expect(texts("status")).toEqual(status === null ? [] : [matching(status)]);
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
  expect(bubble?.parentElement?.querySelector('[role="status"]')?.textContent).toBe(status);
  const dismiss = buttonIn(bubble?.parentElement, "Dismiss");
  expect(dismiss).toBeDefined();
  await act(async () => dismiss?.click());
  expect(container.querySelector(`.agentplane-user-bubble[data-message-phase="${phase}"]`)).toBeNull();
  expect(new LocalCommands(THREAD.id).isDismissed("test-entity")).toBe(true);
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
    // The retained call adds nothing of its own beside the failure it carries.
    expect(badgeLabels(run).sort()).toEqual(["Failed", "Interrupted", "Retention unknown"]);
  });

  it("gives a collapsed run of retained, finished steps a header with no badges", async () => {
    const [run] = await renderHistory(
      [
        testItem(1, ItemKind.TOOL_CALL, {
          tool_name: "test-read",
          completion: "tool",
          tool_succeeded: true,
          recovery: RecoveryDisposition.RETAINED,
        }),
        testItem(
          2,
          ItemKind.REASONING,
          { recovery: RecoveryDisposition.RETAINED },
          { textRef: reference("test-entity-2", "text") }
        ),
        testItem(3, ItemKind.TOOL_CALL, {
          tool_name: "test-shell",
          completion: "tool",
          tool_succeeded: true,
          recovery: RecoveryDisposition.RETAINED,
        }),
      ],
      false,
      { "test-entity-2:text": "Kept in context" }
    );
    const summary = run.querySelector("summary")!;
    expect(summary.textContent).toContain("2 tool calls, 1 reasoning step");
    expect(badgeLabels(run)).toEqual([]);

    await toggle(summary);
    expect(run.textContent).toContain("test-read");
    expect(badgeLabels(run)).toEqual([]);
  });
});
