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
    initializing: false,
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
  vi.restoreAllMocks();
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
    useCommandRows: (ids) =>
      state.window?.rows.filter((row) => row.entityKind === "command" && ids.includes(row.entityId)) ?? [],
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

it("shows inferred model activity in the composer controls, not in a separate row", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:10:00Z"));
  sharedThread = { last_model_activity_at: "2026-01-01T12:02:00Z", active_turn_id: "running" };
  const container = await render();
  const activity = container.querySelector<HTMLElement>(".agentplane-composer-model-activity");
  expect(activity?.textContent).toContain("Model activity: 8m ago");
  expect(activity?.parentElement?.classList.contains("agentplane-composer-controls")).toBe(true);
  expect(activity?.nextElementSibling?.classList.contains("agentplane-composer-send")).toBe(true);
  expect(container.querySelector(".agentplane-thread-tail-model-activity")?.textContent).toContain("8m ago");
  expect(activity?.getAttribute("title")).toContain("not a measured provider request or cache hit");
  expect(container.querySelector(".agentplane-sidebar-row-activity")).toBeNull();
});

it.each([
  ["2026-01-01T12:09:30Z", "just now"],
  ["2026-01-01T10:10:00Z", "2h ago"],
  ["2025-12-29T12:10:00Z", "3d ago"],
  ["2026-01-01T12:12:00Z", "unknown"],
  ["invalid", "unknown"],
])("formats model activity at %s as %s", async (at, expected) => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:10:00Z"));
  sharedThread = { last_model_activity_at: at };
  const container = await render();
  expect(container.querySelector(".agentplane-composer-model-activity")?.textContent).toContain(expected);
});

it("shows unknown at the composer before any model-originated event", async () => {
  const container = await render();
  expect(container.querySelector(".agentplane-composer-model-activity")?.textContent).toContain("unknown");
});

it("sends the draft on Enter and clears it", async () => {
  const container = await render();
  const field = composer(container);
  await type(field, "hello");
  await press(field, {});
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "hello" } }]);
  expect(field.value).toBe("");
  const bubble = container.querySelector<HTMLElement>('.agentplane-user-bubble[data-message-phase="local"]');
  expect(bubble?.querySelector(".agentplane-verbatim")?.textContent).toBe("hello");
  expect(bubble?.parentElement?.querySelector('[role="status"] button')?.getAttribute("aria-label")).toBe(
    "Saved in browser · waiting for runner to accept"
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

function pendingRow(container: Element, commandId: string): HTMLElement {
  const row = container.querySelector<HTMLElement>(`[data-command-id="${commandId}"]`);
  if (!row) throw new Error(`Missing pending command ${commandId}`);
  return row;
}

function progressLabel(container: HTMLDivElement, commandId: string): string | null | undefined {
  return pendingRow(container, commandId).querySelector(".agentplane-command-progress-hit")?.getAttribute("aria-label");
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
  expect(progressLabel(container, "retained-admitted")).toBe("Runner accepted · waiting for agent confirmation");
  expect(
    [...pendingRow(container, "retained-admitted").querySelectorAll(".agentplane-command-light")].map((light) =>
      light.getAttribute("data-state")
    )
  ).toEqual(["done", "done", "waiting"]);
});

it("opens compact command detail on touch and closes on blur or Escape", async () => {
  new LocalCommands(THREAD.id).remember(message("touch"));
  const container = await render();
  const progress = pendingRow(container, "touch").querySelector<HTMLElement>(".agentplane-command-progress");
  const indicator = progress?.querySelector<HTMLButtonElement>(".agentplane-command-progress-hit");
  expect(indicator?.getAttribute("aria-label")).toBeTruthy();
  expect(progress?.querySelectorAll(".agentplane-command-light")).toHaveLength(3);
  expect(progress?.hasAttribute("data-touch-open")).toBe(false);
  const tap = () => {
    const event = new Event("pointerdown", { bubbles: true });
    Object.defineProperty(event, "pointerType", { value: "touch" });
    indicator?.dispatchEvent(event);
  };
  await act(async () => tap());
  expect(progress?.getAttribute("data-touch-open")).toBe("true");
  await act(async () => tap());
  expect(progress?.hasAttribute("data-touch-open")).toBe(false);
  await act(async () => tap());
  await act(async () => {
    indicator?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  expect(progress?.hasAttribute("data-touch-open")).toBe(false);
  await act(async () => {
    indicator?.focus();
    tap();
  });
  expect(progress?.getAttribute("data-touch-open")).toBe("true");
  await act(async () => indicator?.blur());
  expect(progress?.hasAttribute("data-touch-open")).toBe(false);
});

it("lets the browser cancel only a locally queued, never-sent message", async () => {
  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  new LocalCommands(THREAD.id).remember(message("unsent"));
  const container = await render();
  expect(vi.mocked(command)).not.toHaveBeenCalled();
  const row = pendingRow(container, "unsent");
  expect(buttonIn(row, "Cancel")).toBeDefined();
  expect(buttonIn(row, "Retry")).toBeUndefined();
  await act(async () => buttonIn(row, "Cancel")?.click());
  expect(new LocalCommands(THREAD.id).getSnapshot().commands).toEqual([]);
  online.mockReturnValue(true);
  await act(async () => window.dispatchEvent(new Event("online")));
  expect(vi.mocked(command)).not.toHaveBeenCalled();
});

it("does not offer Cancel once an offline command has begun its first HTTP attempt", async () => {
  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  new LocalCommands(THREAD.id).remember(message("queued"));
  const container = await render();
  expect(buttonIn(pendingRow(container, "queued"), "Cancel")).toBeDefined();
  online.mockReturnValue(true);
  await act(async () => window.dispatchEvent(new Event("online")));
  expect(sentIds()).toEqual(["queued"]);
  expect(new LocalCommands(THREAD.id).cancelUnsent("queued")).toBe(false);
  expect(buttonIn(pendingRow(container, "queued"), "Cancel")).toBeUndefined();
});

it("shows a delivery that outlives its deadline as unconfirmed and retriable", async () => {
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
  expect(progressLabel(container, "hung")).toContain("Saved in browser · waiting for runner to accept");
  expect(
    [...pendingRow(container, "hung").querySelectorAll(".agentplane-command-light")].map((light) =>
      light.getAttribute("data-state")
    )
  ).toEqual(["done", "waiting", "future"]);
  expect(pendingRow(container, "hung").querySelectorAll("[data-state=waiting]")).toHaveLength(1);

  await act(async () => expire(new DOMException("signal timed out", "TimeoutError")));
  expect(progressLabel(container, "hung")).toContain("Runner receipt unconfirmed");
  expect(pendingRow(container, "hung").querySelector('[data-state="uncertain"]')).not.toBeNull();
  expect(progressLabel(container, "hung")).toContain("signal timed out");
  expect(sentIds()).toEqual(["hung"]);

  await act(async () => retry(container, "hung").click());
  expect(sentIds()).toEqual(["hung", "hung"]);
  expect(progressLabel(container, "hung")).toContain("Runner accepted · waiting for agent confirmation");
  expect(progressLabel(container, "hung")).not.toContain("signal timed out");
});

it("redelivers an unconfirmed command when the browser comes back online", async () => {
  new LocalCommands(THREAD.id).remember(message("offline"));
  vi.mocked(command)
    .mockRejectedValueOnce(new Error("the sandbox's runner is not answering"))
    .mockImplementationOnce(admit);
  const container = await render();
  expect(progressLabel(container, "offline")).toContain("the sandbox's runner is not answering");
  expect(sentIds()).toEqual(["offline"]);

  await act(async () => window.dispatchEvent(new Event("online")));
  expect(sentIds()).toEqual(["offline", "offline"]);
  expect(progressLabel(container, "offline")).toContain("Runner accepted · waiting for agent confirmation");
});

it("moves a local control into chronological history once projected and retains its effected outcome", async () => {
  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  new LocalCommands(THREAD.id).remember(
    create(CommandSchema, {
      commandId: "inline-model",
      operation: { case: "changeModel", value: { model: "next-model" } },
    })
  );
  const container = await render();
  const local = pendingRow(container, "inline-model");
  expect(local.closest('[aria-label="Thread history"]')).not.toBeNull();
  expect(local.querySelector('[data-stage="local"]')).not.toBeNull();

  const projected = entity(
    "command",
    {
      operation: "change_model",
      outcome: "effected",
      outcome_cursor: "2",
      outcome_reason: null,
      requested_value: "next-model",
    },
    {}
  );
  projected.entityId = "inline-model";
  projected.cursor = "2";
  await rerender(container, threadState({ rows: [viewState(), projected] }));
  expect(container.querySelectorAll('[data-command-id="inline-model"]')).toHaveLength(0);
  const [historyEntry] = await renderHistory([projected], false);
  const settled = historyEntry.querySelector<HTMLElement>('[data-command-id="inline-model"]')!;
  expect(settled).not.toBeNull();
  expect(settled.querySelector(".agentplane-command-check")).not.toBeNull();
  expect(settled.textContent).toContain("Change model to next-model");
  expect(new LocalCommands(THREAD.id).getSnapshot().commands).toHaveLength(0);
  online.mockRestore();
});

it("shows a server-only pending command as saved, not as a local delivery", async () => {
  const [section] = await renderHistory(
    [
      entity(
        "command",
        {
          operation: "change_model",
          outcome: "pending",
          outcome_cursor: null,
          outcome_reason: null,
          requested_value: "next-model",
        },
        {}
      ),
    ],
    false
  );
  const pending = pendingRow(section, "test-entity");
  expect(pending.textContent).toContain("Change model to next-model");
  expect(pending.querySelector(".agentplane-command-progress-hit")?.getAttribute("aria-label")).toBe(
    "Runner accepted · waiting for model change"
  );
  expect(pending.textContent).not.toContain("Saved in browser");
  expect(pending.querySelector('[data-stage="admitted"]')).not.toBeNull();
  expect([...section.querySelectorAll("button")].some((button) => button.textContent === "Retry")).toBe(false);
});

it("shows the failure and reason for a server-only command", async () => {
  const [section] = await renderHistory(
    [
      entity(
        "command",
        {
          operation: "change_model",
          outcome: "failed",
          outcome_cursor: "1",
          outcome_reason: "model unavailable",
          requested_value: null,
        },
        {}
      ),
    ],
    false
  );
  const pending = pendingRow(section, "test-entity");
  expect(pending.querySelector(".agentplane-command-progress-hit")?.getAttribute("aria-label")).toBe(
    "Failed: model unavailable"
  );
  expect(pending.querySelector('[data-stage="failed"] [data-state="failed"]')).not.toBeNull();
});

it("renders a server-only no-op control as a settled label, not a progress light", async () => {
  const [section] = await renderHistory(
    [
      entity(
        "command",
        {
          operation: "change_model",
          outcome: "noop",
          outcome_cursor: "1",
          outcome_reason: "model already selected",
          requested_value: "current-model",
        },
        {}
      ),
    ],
    false
  );
  const row = pendingRow(section, "test-entity");
  expect(row.textContent).toContain("Change model to current-model");
  expect(row.querySelector(".agentplane-command-progress")).toBeNull();
  expect(row.querySelector('[role="status"]')?.textContent).toBe("No-op: model already selected");
  expect(row.querySelector('[role="status"]')?.getAttribute("title")).toBe("No-op: model already selected");
});

it.each([
  ["failed", "failed", "Failed: runner unavailable"],
  ["noop", "noop", "No-op: harness was stopping"],
  ["effected", "confirmed", "Agent confirmed message"],
] as const)("shows a server-only %s input as a persistent right-side message", async (outcome, phase, status) => {
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
            requested_value: null,
          },
          { inputRef: reference("failed-input", "command_input") }
        ),
      ],
    })
  );
  const bubble = container.querySelector<HTMLElement>(`.agentplane-user-bubble[data-message-phase="${phase}"]`);
  const plainOutcome = bubble?.parentElement?.querySelector(".agentplane-command-noop");
  expect(plainOutcome?.textContent).toBe(outcome === "noop" ? status : undefined);
  expect(plainOutcome?.getAttribute("title")).toBe(outcome === "noop" ? status : undefined);
  expect(bubble?.parentElement?.querySelector(".agentplane-command-progress") === null).toBe(outcome === "noop");
  expect(bubble?.parentElement?.querySelectorAll(".agentplane-command-light")).toHaveLength(
    outcome === "effected" || outcome === "noop" ? 0 : 3
  );
  expect(bubble?.parentElement?.querySelectorAll(".agentplane-command-check")).toHaveLength(
    outcome === "effected" ? 1 : 0
  );
  expect(bubble?.parentElement?.querySelector(".agentplane-command-progress-hit")?.getAttribute("aria-label")).toBe(
    outcome === "noop" ? undefined : status
  );
  expect(bubble?.parentElement?.querySelector("button[aria-label='Dismiss']")).toBeNull();
  expect(container.querySelector(`.agentplane-user-bubble[data-message-phase="${phase}"]`)).not.toBeNull();
});

it("keeps a still-pending sent message out of the pending-commands box, since it renders inline instead", async () => {
  const container = await render(
    threadState({
      rows: [
        viewState(),
        entity(
          "command",
          {
            operation: "submit_input",
            outcome: "pending",
            outcome_cursor: null,
            outcome_reason: null,
            requested_value: null,
          },
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
  bodies: Record<string, string> = {},
  inputEchoes: ReadonlyMap<string, string> = new Map(),
  loading: ReadonlySet<string> = new Set(),
  onInputEchoLoaded?: (commandId: string) => void
): Promise<HTMLElement[]> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <ThreadSyncContext.Provider value={serving(new Map(Object.entries(bodies)), new Map(), loading)}>
          <RetainedDisclosureProvider>
            {historyRows(segments).map((row) => (
              <section key={rowKey(row)}>
                <HistoryRowView
                  threadId="test-thread"
                  row={row}
                  live={() => live}
                  inputEchoes={inputEchoes}
                  onInputEchoLoaded={onInputEchoLoaded}
                />
              </section>
            ))}
          </RetainedDisclosureProvider>
        </ThreadSyncContext.Provider>
      </MantineProvider>
    )
  );
  return [...container.querySelectorAll("section")];
}

function pendingInput(id: string, cursor: string): ThreadEntity {
  const row = entity(
    "command",
    {
      operation: "submit_input",
      outcome: "pending",
      outcome_cursor: null,
      outcome_reason: null,
      requested_value: null,
    },
    { inputRef: reference(id, "command_input") }
  );
  row.entityId = id;
  row.cursor = cursor;
  row.revisionCursor = cursor;
  return row;
}

it("uses this browser's submitted text while the echo loads and leaves remote input loading", async () => {
  const localId = "local-input";
  const remoteId = "remote-input";
  const sections = await renderHistory(
    [pendingInput(localId, "1"), pendingInput(remoteId, "2")],
    false,
    {},
    new Map([[localId, "Text submitted by this browser."]]),
    new Set([`${localId}:command_input`, `${remoteId}:command_input`])
  );
  const local = sections.find((section) => section.querySelector(`[data-command-id="${localId}"]`))!;
  const remote = sections.find((section) => section.querySelector(`[data-command-id="${remoteId}"]`))!;

  expect(local.querySelector(".agentplane-verbatim")?.textContent).toBe("Text submitted by this browser.");
  expect(local.textContent).not.toContain("Loading complete revision");
  expect(remote.textContent).toContain("Loading complete revision");
});

it("replaces locally known text with the service echo once that payload arrives", async () => {
  const onInputEchoLoaded = vi.fn();
  const [section] = await renderHistory(
    [pendingInput("local-input", "1")],
    false,
    { "local-input:command_input": "Text returned by the service." },
    new Map([["local-input", "Text submitted by this browser."]]),
    new Set(),
    onInputEchoLoaded
  );

  expect(section.querySelector(".agentplane-verbatim")?.textContent).toBe("Text returned by the service.");
  expect(onInputEchoLoaded).toHaveBeenCalledWith("local-input");
});

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
  const summary = run.querySelector(".agentplane-disclosure-summary")!;
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
  expect(live.querySelector('.agentplane-disclosure-summary [role="img"]')?.getAttribute("aria-label")).toBe(
    "Streaming"
  );
  const [retained] = await renderHistory(segments, false);
  expect(retained.querySelector('.agentplane-disclosure-summary [role="img"]')?.getAttribute("aria-label")).toBe(
    "Incomplete"
  );
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
  expect(reasoning.querySelector(".agentplane-step-details")).toBeNull();
  expect(reasoning.querySelector(".agentplane-step-title")?.textContent).toBe("Reasoning");
  expect(reasoning.textContent).toContain("Reasoning preview body");
  expect(reasoning.querySelector(".mantine-Badge-root")).toBeNull();
  expect(answer.textContent?.trim()).toBe("Test body of test-entity-2");
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
    const summary = run.querySelector(".agentplane-disclosure-summary")!;
    expect(summary.textContent).toContain("2 tool calls, 1 reasoning step");
    expect(badgeLabels(run)).toEqual([]);

    await toggle(summary);
    expect(run.textContent).toContain("test-read");
    expect(badgeLabels(run)).toEqual([]);
  });
});
