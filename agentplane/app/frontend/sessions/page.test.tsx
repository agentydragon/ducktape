import type * as ThreadClientModule from "./client";
// @vitest-environment happy-dom
import {
  ThreadPayloadField,
  ThreadCommandOutcome,
  ThreadFeedStatus,
  FeedStatus,
  SessionSummarySchema,
  type ThreadEntity,
} from "../../../history_service/api/history/history_service_pb";
import { AutosuspendBlockerSchema } from "../../../proto/environment_lifecycle_pb";
import { DurationSchema, TimestampSchema, timestampFromDate } from "@bufbuild/protobuf/wkt";
import { changedSandbox, readySandbox, suspendedSandbox } from "../testing/sandbox";
import {
  SandboxPhase,
  ReadinessSchema,
  ReadinessState,
  EnvironmentDescriptorSchema,
  type EnvironmentDescriptor,
} from "../../../environments/v1/environment_service_pb";

import { create, toJson } from "@bufbuild/protobuf";
import {
  FrontendConfigSchema,
  TraceSearchSettingsSchema,
  type LangfuseSearchSettings,
  type TraceSearchSettings,
} from "../../app_service_pb";
import { TEST_REASONING_EFFORTS, testModelCatalog } from "./testing/model_catalog";
import { MantineProvider } from "@mantine/core";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandSchema, Operation, type Command } from "../../../proto/command_pb";
import { EventEntrySchema, type EventEntry } from "../../../proto/event_log_pb";
import { ItemKind, RecoveryDisposition } from "../../../proto/event_pb";
import { HarnessState, LaunchOutcome, SetupState } from "../../../proto/session_pb";
import { HarnessAttachment_State } from "../../../harness_host/harness_host_pb";
import {
  SessionSchema,
  SessionStatus_Materialization,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import type * as SandboxClientModule from "../sandboxes/client";
import { LANGFUSE_SEARCH_SETTINGS } from "../observability/langfuse_search_fixture";
import * as appConfigClient from "../app_config/client";
import {
  type CommandSubmission,
  dispatchFromJson,
  submitInput,
  changeModel,
  changeReasoningEffort,
  interruptTurn,
  getThread,
  listThreadMetadata,
} from "./client";
import { resumeSandbox } from "../sandboxes/client";
import { historyRows, rowKey } from "./history_rows";
import { SandboxesLiveProvider, useRequiredSandboxesLive } from "../sandboxes/live";
import { ThreadsLiveProvider, useRequiredThreadsLive } from "./live";
import { LocalCommands } from "./local_commands";
import { STREAMING_CURSOR } from "../../../../util/frontend/markdown";
import { ThreadPage } from "./page";
import { HistoryRowView } from "./history_row_views";
import { RetainedDisclosureProvider } from "../../../../util/frontend/transcript/retained_disclosures";
import { DEGRADED_AFTER_MS, STALE_AFTER_MS } from "../transport/stream_status";
import { testItem } from "./testing/entity";
import {
  entity,
  environmentDownSession,
  observedHostedSession,
  reference,
  serving,
  THREAD,
  threadState,
  threadView,
  toggle,
  viewState,
  type ThreadViewInit,
} from "./testing/state";
import { ThreadSyncContext, type ThreadState, type ThreadSync } from "./thread_sync";
import { TopbarContext } from "../shell/topbar";
import { reconnected, serveEventStreams, ServedWatch, stubWatchFetch } from "../testing/watch";

const COMMAND_TARGET = { sandbox: THREAD.summary.sandbox!.name, sandboxUid: "20000000-0000-4000-8000-000000000001" };

type ThreadView = ThreadClientModule.ThreadView;

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof ThreadClientModule>()),
  submitInput: vi.fn(),
  changeModel: vi.fn(),
  changeReasoningEffort: vi.fn(),
  interruptTurn: vi.fn(),
  getThread: vi.fn(),
  listThreadMetadata: vi.fn(),
}));
vi.mock("../sandboxes/client", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxClientModule>()),
  resumeSandbox: vi.fn(),
}));

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
type Inventory = EnvironmentDescriptor[] | null;

function inventorySandbox(scenario: "ready" | "suspended" = "ready", wakeOnSend = false): EnvironmentDescriptor {
  const uid = "20000000-0000-4000-8000-000000000001";
  const sandbox =
    scenario === "ready"
      ? readySandbox(THREAD.summary.sandbox!.name, uid)
      : suspendedSandbox(THREAD.summary.sandbox!.name, uid);
  if (sandbox.kubernetesDiagnostics?.pod) sandbox.kubernetesDiagnostics.pod.uid = "test-pod-1";
  sandbox.configuration!.wakeOnSend = wakeOnSend;
  return sandbox;
}

function autosuspendSandbox(idle_suspend_at: string | null): EnvironmentDescriptor {
  return changedSandbox(inventorySandbox(), {
    configuration: { idleSuspendAfter: create(DurationSchema, { seconds: 900n }) },
    observedState: {
      idleSuspendAt: idle_suspend_at === null ? undefined : timestampFromDate(new Date(idle_suspend_at)),
    },
  });
}
let sandboxes: Inventory = [];
let inventoryFresh = true;
let inventoryDrops = false;
let sharedFeed: FeedStatus = FeedStatus.ACTIVE;
let sharedThread: ThreadViewInit = {};
let eventStreamUrls: string[] = [];
const TRACE_SEARCH_SETTINGS: TraceSearchSettings = create(TraceSearchSettingsSchema, {
  grafanaBaseUrl: "https://grafana.test.invalid",
  tempoDatasourceUid: "tempo-test",
  deploymentEnvironment: "agentplane-test",
});
let traceSearchSettings: TraceSearchSettings | null = TRACE_SEARCH_SETTINGS;
let langfuseSearchSettings: LangfuseSearchSettings | null = LANGFUSE_SEARCH_SETTINGS;
// The Dispatcher watches the page opened, which a test answers itself.
let dispatchWatches: ServedWatch[] = [];
let threadRows: ThreadView[] | null = null;
let noticeInboxes: Array<{ inboxId: string; sessionId: string; queued: number; unacknowledged: number }> = [];

function sharedThreadView(): ThreadView {
  const sharedSummary = sharedThread.summary;
  const attachedOverride =
    sharedSummary && Object.hasOwn(sharedSummary, "attached") ? sharedSummary.attached : THREAD.summary.attached;
  const attached = attachedOverride === undefined ? undefined : { ...THREAD.summary.attached, ...attachedOverride };
  return threadView({
    metadata: { ...THREAD.metadata, ...sharedThread.metadata },
    summary: {
      ...THREAD.summary,
      ...sharedSummary,
      feedStatus: sharedSummary?.feedStatus ?? sharedFeed,
      attached,
    },
    ...(Object.hasOwn(sharedThread, "session") ? { session: sharedThread.session } : {}),
  });
}

function dispatcherSnapshotSessions(): NonNullable<ThreadView["session"]>[] {
  const candidates = [THREAD, ...(threadRows ?? [sharedThreadView()])];
  const unique = new Map(candidates.map((thread) => [thread.metadata.threadId, thread]));
  return [...unique.values()].flatMap((thread) => (thread.session ? [thread.session] : []));
}

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
  sharedFeed = FeedStatus.ACTIVE;
  sharedThread = {};
  eventStreamUrls = [];
  traceSearchSettings = TRACE_SEARCH_SETTINGS;
  langfuseSearchSettings = LANGFUSE_SEARCH_SETTINGS;
  dispatchWatches = stubWatchFetch().served;
  threadRows = null;
  noticeInboxes = [];
  vi.spyOn(appConfigClient, "getFrontendConfig").mockImplementation(async () =>
    create(FrontendConfigSchema, {
      traceSearch: traceSearchSettings ?? undefined,
      langfuseSearch: langfuseSearchSettings ?? undefined,
    })
  );
  vi.mocked(getThread).mockImplementation(async () => sharedThreadView());
  vi.spyOn(appConfigClient, "listModels").mockResolvedValue(
    testModelCatalog([{ model: "test-model", displayName: "Test Model", reasoningEfforts: TEST_REASONING_EFFORTS }], [])
  );
  for (const operation of [submitInput, changeModel, changeReasoningEffort, interruptTurn]) {
    vi.mocked(operation).mockReturnValue(new Promise(() => {}));
  }
  const served = serveEventStreams(
    (stream) => {
      const { pathname, search } = stream.url;
      eventStreamUrls.push(`${pathname}${search}`);
      if (/^\/upstream\/notifications\/v1\/sessions\/[^/]+\/inbox:watch$/.test(pathname)) {
        const notice = noticeInboxes.find((item) => pathname.includes(encodeURIComponent(item.sessionId)));
        stream.send(
          notice
            ? {
                inbox: {
                  id: notice.inboxId,
                  sessionId: notice.sessionId,
                  delivery: {
                    unannouncedCount: notice.queued,
                    pendingAcknowledgementCount: notice.queued + notice.unacknowledged,
                  },
                },
              }
            : { noInbox: {} }
        );
        return;
      }
      if (sandboxes === null || reconnected(served, stream)) return;
      stream.send({
        snapshot: { environments: sandboxes.map((sandbox) => toJson(EnvironmentDescriptorSchema, sandbox)) },
      });
      stream.send({ snapshotComplete: {} });
      if (!inventoryFresh) stream.drop();
      if (inventoryDrops) stream.drop();
    },
    undefined,
    (path) =>
      /^\/upstream\/notifications\/v1\/sessions\/[^/]+\/inbox:watch$/.test(path) ||
      path === "/upstream/environments/v1/environments:watch"
  );
  const sandboxFetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (new Headers(init?.headers).get("Accept") === "application/x-ndjson") {
      const url = new URL(String(input), window.location.href);
      if (
        url.pathname === "/upstream/dispatcher/v1/sessions:watch" ||
        url.pathname === "/upstream/app/v1/threads:watch" ||
        url.pathname === "/upstream/history/v1/session-summaries:watchSelected"
      ) {
        const stream = new ServedWatch(String(input), init?.signal ?? undefined);
        if (url.pathname === "/upstream/dispatcher/v1/sessions:watch") {
          for (const [index, session] of dispatcherSnapshotSessions().entries())
            stream.send({ session: toJson(SessionSchema, session), resumeToken: `test-session-${index}` });
          stream.send({ caughtUp: {}, resumeToken: "test-snapshot-token" });
        } else if (url.pathname === "/upstream/app/v1/threads:watch") {
          stream.send({ changed: {}, updatesConnected: true });
        } else {
          const ids = url.searchParams.getAll("session_ids");
          stream.send({
            snapshot: {
              summaries: (threadRows ?? [sharedThreadView()])
                .filter((row) => ids.includes(row.metadata.threadId))
                .map((row) => toJson(SessionSummarySchema, row.summary)),
            },
          });
        }
        return Promise.resolve(stream.response);
      }
    }
    return sandboxFetch(input, init);
  });
  vi.mocked(listThreadMetadata).mockImplementation(
    async () => new Map((threadRows ?? [sharedThreadView()]).map((row) => [row.metadata.threadId, row.metadata]))
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
  // The real shell topbar (app.tsx) isn't mounted here, so ThreadPage's title/menu need
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

it("offers a Grafana trace link from the thread menu when configured", async () => {
  const link = menuItem(await threadMenuItems(), "Find Thread traces in Grafana");
  expect(link).toBeInstanceOf(HTMLAnchorElement);
  expect(new URL((link as HTMLAnchorElement).href).searchParams.get("panes")).toContain("span.session.id");
});

it("offers a Sandbox-scoped Langfuse link from the active Thread menu", async () => {
  const link = menuItem(await threadMenuItems(), "Sandbox LLM traces in Langfuse");
  expect(link).toBeInstanceOf(HTMLAnchorElement);
  const url = new URL((link as HTMLAnchorElement).href);
  expect(url.searchParams.get("filter")).toBe(
    "metadata;stringObject;attributes.agentplane.namespace;=;agentplane-test," +
      "metadata;stringObject;attributes.agentplane.service_account;=;composer-test"
  );
  expect((link as HTMLAnchorElement).target).toBe("_blank");
});

it("keeps the Langfuse link available from a stale Sandbox snapshot", async () => {
  inventoryFresh = false;
  expect(menuItem(await threadMenuItems(), "Sandbox LLM traces in Langfuse")).toBeDefined();
});

it("hides the Grafana trace link from the thread menu when unconfigured", async () => {
  traceSearchSettings = null;
  expect(menuItem(await threadMenuItems(), "Find Thread traces in Grafana")).toBeUndefined();
});

it("hides only the Langfuse link when Langfuse search is unconfigured", async () => {
  langfuseSearchSettings = null;
  const items = await threadMenuItems();
  expect(menuItem(items, "Sandbox LLM traces in Langfuse")).toBeUndefined();
  expect(menuItem(items, "Find Thread traces in Grafana")).toBeDefined();
});

function TestLiveProviders({ children }: { children: JSX.Element }): JSX.Element {
  return (
    <MemoryRouter>
      <ThreadsLiveProvider openThreadId={null}>
        <SandboxesLiveProvider>
          <WaitForInventory>{children}</WaitForInventory>
        </SandboxesLiveProvider>
      </ThreadsLiveProvider>
    </MemoryRouter>
  );
}

function WaitForInventory({ children }: { children: JSX.Element }): JSX.Element | null {
  const live = useRequiredSandboxesLive();
  return sandboxes === null || live.snapshot !== null ? children : null;
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
      state.window?.rows.filter((row) => row.entity.case === "command" && ids.includes(row.entityId)) ?? [],
    usePayload: () => ({ body: null, error: null, retry: () => {} }),
  };
  return (
    <MantineProvider env="test">
      <ThreadSyncContext.Provider value={sync}>
        <TopbarContext.Provider value={{ title: topbarTitle, actions: topbarActions }}>
          <ThreadPage key={mountKey} threadId={THREAD.metadata.threadId} />
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

async function threadMenuItems(): Promise<Element[]> {
  const container = await render();
  await act(async () => button(container, "More").click());
  return [...document.querySelectorAll('[role="menuitem"]')];
}

function menuItem(items: Element[], text: string): Element | undefined {
  return items.find((node) => node.textContent?.includes(text));
}

function sentOperations(): unknown[] {
  const sent = [
    ...vi.mocked(submitInput).mock.calls.map(([, , text], index) => ({
      order: vi.mocked(submitInput).mock.invocationCallOrder[index],
      operation: { case: "submitInput", value: { text } },
    })),
    ...vi.mocked(changeModel).mock.calls.map(([, , model], index) => ({
      order: vi.mocked(changeModel).mock.invocationCallOrder[index],
      operation: { case: "changeModel", value: { model } },
    })),
    ...vi.mocked(changeReasoningEffort).mock.calls.map(([, , effort], index) => ({
      order: vi.mocked(changeReasoningEffort).mock.invocationCallOrder[index],
      operation: { case: "changeReasoningEffort", value: { effort } },
    })),
    ...vi.mocked(interruptTurn).mock.calls.map(([, , turnId], index) => ({
      order: vi.mocked(interruptTurn).mock.invocationCallOrder[index],
      operation: { case: "interruptTurn", value: { turnId } },
    })),
  ];
  return sent.sort((left, right) => left.order - right.order).map((value) => value.operation);
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
    expect(submitInput).not.toHaveBeenCalled();
  }
);

it("shows inferred model activity with its provider-measurement explanation", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:10:00Z"));
  sharedThread = {
    summary: {
      lastModelActivityAt: timestampFromDate(new Date("2026-01-01T12:02:00Z")),
    },
  };
  const container = await render();
  const activity = container.querySelector<HTMLElement>(".agentplane-composer-model-activity");
  expect(activity?.textContent).toContain("Model activity: 8m ago");
  expect(container.querySelector(".agentplane-thread-tail-model-activity")?.textContent).toContain("8m ago");
  expect(activity?.getAttribute("title")).toContain("not a measured provider request or cache hit");
  expect(container.querySelector(".agentplane-sidebar-row-activity")).toBeNull();
});

it("shows how long the harness has been idle, with the full time on hover", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:10:00Z"));
  sharedThread = { summary: { idleSince: timestampFromDate(new Date("2026-01-01T12:04:00Z")) } };
  const container = await render();
  const idle = container.querySelector(".agentplane-composer-model-activity time");
  expect(idle?.textContent).toBe("6 minutes ago");
  expect(idle?.getAttribute("title")).toBe("2026-01-01T12:04:00.000Z");
});

it("collapses model activity and idle timestamps that are close and display the same age", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:10:00Z"));
  sharedThread = {
    summary: {
      lastModelActivityAt: timestampFromDate(new Date("2026-01-01T11:56:35Z")),
      idleSince: timestampFromDate(new Date("2026-01-01T11:56:55Z")),
    },
  };
  const activity = (await render()).querySelector<HTMLElement>(".agentplane-composer-model-activity");
  expect(activity?.textContent).toBe("Model activity: 13m ago · Idle");
  expect(activity?.querySelector("time")?.getAttribute("title")).toBe("2026-01-01T11:56:55.000Z");
});

it("keeps distinct model activity and idle ages visible", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:10:00Z"));
  sharedThread = {
    summary: {
      lastModelActivityAt: timestampFromDate(new Date("2026-01-01T11:57:00Z")),
      idleSince: timestampFromDate(new Date("2026-01-01T11:54:00Z")),
    },
  };
  const activity = (await render()).querySelector<HTMLElement>(".agentplane-composer-model-activity");
  expect(activity?.textContent).toContain("Model activity: 13m ago · Idle since");
  expect(activity?.querySelector("time")?.getAttribute("title")).toBe("2026-01-01T11:54:00.000Z");
});

it("counts down from the controller deadline on the UI clock without reconnecting inventory", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(Date.parse("2026-01-01T12:00:00Z"));
  sandboxes = [autosuspendSandbox("2026-01-01T12:12:00Z")];
  const container = await render();
  const hint = container.querySelector(".agentplane-composer-autosuspend");
  expect(hint?.textContent).toBe("Sandbox autosuspends in ~12m");
  const inventoryStreams = () => eventStreamUrls.filter((url) => url.startsWith("/upstream/environments/"));
  const streams = inventoryStreams().length;

  await act(async () => vi.advanceTimersByTimeAsync(60_000));

  expect(hint?.textContent).toBe("Sandbox autosuspends in ~11m");
  expect(inventoryStreams()).toHaveLength(streams);
  expect(hint?.getAttribute("title")).toContain("Next suspension attempt: 2026-01-01T12:12:00.000Z");
});

it("reports the autosuspend policy while no idle deadline is active", async () => {
  sandboxes = [autosuspendSandbox(null)];
  const container = await render();
  expect(container.querySelector(".agentplane-composer-autosuspend")?.textContent).toBe("Autosuspend after 15m idle");
});

it("shows an unfamiliar autosuspend blocker without breaking the inventory", async () => {
  sandboxes = [
    changedSandbox(autosuspendSandbox(null), {
      observedState: { autosuspendBlockers: [create(AutosuspendBlockerSchema, { reason: "FutureReason" })] },
    }),
  ];
  const container = await render();
  expect(container.querySelector(".agentplane-composer-autosuspend")?.textContent).toBe(
    "Autosuspend waiting: FutureReason"
  );
});

it("explains every runner blocker and links sessions outside the loaded thread listing", async () => {
  const unlistedSession = "00000000-0000-4000-8000-000000000099";
  sandboxes = [
    changedSandbox(autosuspendSandbox(null), {
      observedState: {
        autosuspendBlockers: [
          create(AutosuspendBlockerSchema, { reason: "ActiveTurn", sessionId: THREAD.metadata.threadId }),
          create(AutosuspendBlockerSchema, { reason: "PendingCommand", sessionId: unlistedSession }),
          create(AutosuspendBlockerSchema, { reason: "Setup", sessionId: unlistedSession }),
          create(AutosuspendBlockerSchema, { reason: "Activation", sessionId: unlistedSession }),
          create(AutosuspendBlockerSchema, { reason: "Bootstrap", sessionId: undefined }),
          create(AutosuspendBlockerSchema, { reason: "Admission", sessionId: undefined }),
        ],
      },
    }),
  ];
  const container = await render();
  const streams = eventStreamUrls.length;
  const hint = container.querySelector(".agentplane-composer-autosuspend");
  expect(hint?.textContent).toBe("Sandbox in active use by Session 00000000 · Details");
  expect(hint?.querySelector("a")?.getAttribute("href")).toBe(`/threads/${unlistedSession}`);
  await act(async () => hint?.querySelector("button")?.click());
  const details = document.querySelector('[role="dialog"]');
  expect(details?.textContent).toContain("Setup running");
  expect(details?.textContent).toContain("Session starting");
  expect(details?.textContent).toContain("Sandbox bootstrap running");
  expect(details?.textContent).toContain("Command being admitted");
  expect(details?.querySelector(`a[href="/threads/${THREAD.metadata.threadId}"]`)?.textContent).toBe(
    THREAD.metadata.name
  );
  expect(details?.querySelector(`a[href="/threads/${unlistedSession}"]`)?.textContent).toBe("Session 00000000");
  expect(eventStreamUrls).toHaveLength(streams);
});

it.each([null, "2026-01-01T12:12:00Z"])(
  "links the busy sibling from an idle thread even with a retained deadline (%s)",
  async (deadline) => {
    const sibling = threadView({
      metadata: { ...THREAD.metadata, threadId: "00000000-0000-4000-8000-000000000098", name: "Busy sibling" },
      summary: THREAD.summary,
      session: observedHostedSession("00000000-0000-4000-8000-000000000098", { activeTurnId: "sibling-turn" }),
    });
    const active = threadView({
      metadata: THREAD.metadata,
      summary: { ...THREAD.summary, feedStatus: FeedStatus.ENDED, attached: { harnessState: HarnessState.STOPPED } },
      session: observedHostedSession(THREAD.metadata.threadId, { activeTurnId: "current-turn" }),
    });
    threadRows = [active, sibling];
    sharedFeed = FeedStatus.ENDED;
    sandboxes = [
      changedSandbox(autosuspendSandbox(deadline), {
        observedState: {
          autosuspendBlockers: [
            create(AutosuspendBlockerSchema, { reason: "ActiveTurn", sessionId: THREAD.metadata.threadId }),
            create(AutosuspendBlockerSchema, { reason: "ActiveTurn", sessionId: sibling.metadata.threadId }),
          ],
        },
      }),
    ];
    const container = await render();
    const hint = container.querySelector(".agentplane-composer-autosuspend");
    expect(hint?.textContent).toBe("Sandbox in active use by Busy sibling · Details");
    expect(hint?.querySelector("a")?.getAttribute("href")).toBe(`/threads/${sibling.metadata.threadId}`);
    expect(hint?.textContent).not.toContain("autosuspends in");
  }
);

it("shows the current session's busy reason instead of a retained deadline", async () => {
  sandboxes = [
    changedSandbox(autosuspendSandbox("2026-01-01T12:12:00Z"), {
      observedState: {
        autosuspendBlockers: [
          create(AutosuspendBlockerSchema, { reason: "ActiveTurn", sessionId: THREAD.metadata.threadId }),
        ],
      },
    }),
  ];
  sharedThread = { session: observedHostedSession(THREAD.metadata.threadId, { activeTurnId: "current-turn" }) };
  const hint = (await render()).querySelector(".agentplane-composer-autosuspend");
  expect(hint?.textContent).toBe("Autosuspend waiting: Turn running");
  expect(hint?.querySelector("a")).toBeNull();
});

it.each([
  ["stale", false, 900, "Autosuspend timing unavailable"],
  ["disabled", true, null, "Autosuspend off"],
] as const)("does not present %s blockers as currently preventing autosuspend", async (_case, fresh, after, text) => {
  inventoryFresh = fresh;
  sandboxes = [
    changedSandbox(autosuspendSandbox(null), {
      configuration: {
        idleSuspendAfter: after === null ? undefined : create(DurationSchema, { seconds: BigInt(after) }),
      },
      observedState: {
        autosuspendBlockers: [
          create(AutosuspendBlockerSchema, { reason: "ActiveTurn", sessionId: THREAD.metadata.threadId }),
        ],
      },
    }),
  ];
  const hint = (await render()).querySelector(".agentplane-composer-autosuspend");
  expect(hint?.textContent).toBe(text);
  expect(hint?.tagName).not.toBe("BUTTON");
});

it("explains bootstrap work before the Sandbox becomes ready", async () => {
  sandboxes = [
    changedSandbox(autosuspendSandbox(null), {
      observedState: {
        readiness: create(ReadinessSchema, { state: ReadinessState.NOT_READY }),
        autosuspendBlockers: [create(AutosuspendBlockerSchema, { reason: "Bootstrap", sessionId: undefined })],
      },
      kubernetesDiagnostics: { sandboxStatus: { conditions: [{ type: "Ready", status: "False" }] } },
    }),
  ];
  const hint = (await render()).querySelector(".agentplane-composer-autosuspend");
  expect(hint?.textContent).toBe("Autosuspend waiting: Sandbox bootstrap running");
});

it("reports stale timing as unavailable and does not call a missing loading sandbox disabled", async () => {
  sandboxes = [autosuspendSandbox("2026-01-01T12:12:00Z")];
  inventoryFresh = false;
  const stale = await render();
  expect(stale.querySelector(".agentplane-composer-autosuspend")?.textContent).toBe("Autosuspend timing unavailable");

  sandboxes = null;
  const loading = await render();
  expect(loading.querySelector(".agentplane-composer-autosuspend")).toBeNull();
});

it("handles a past deadline without showing a negative countdown", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:00:00Z"));
  sandboxes = [autosuspendSandbox("2026-01-01T11:59:00Z")];
  const container = await render();
  expect(container.querySelector(".agentplane-composer-autosuspend")?.textContent).toBe("Autosuspend pending");
});

it.each([
  ["2026-01-01T12:09:30Z", "just now"],
  ["2026-01-01T10:10:00Z", "2h ago"],
  ["2025-12-29T12:10:00Z", "3d ago"],
  ["2026-01-01T12:12:00Z", "unknown"],
  ["invalid", "unknown"],
])("formats model activity at %s as %s", async (at, expected) => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:10:00Z"));
  sharedThread = {
    summary: {
      lastModelActivityAt:
        at === "invalid" ? create(TimestampSchema, { seconds: 999999999999999999n }) : timestampFromDate(new Date(at)),
    },
  };
  const container = await render();
  expect(container.querySelector(".agentplane-composer-model-activity")?.textContent).toContain(expected);
});

it("shows unknown at the composer before any model-originated event", async () => {
  const container = await render();
  expect(container.querySelector(".agentplane-composer-model-activity")?.textContent).toContain("unknown");
});

it("keeps inferred model activity but hides History idle time while Session activity is unknown", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:10:00Z"));
  sharedThread = {
    summary: {
      lastModelActivityAt: timestampFromDate(new Date("2026-01-01T12:02:00Z")),
      idleSince: timestampFromDate(new Date("2026-01-01T12:04:00Z")),
    },
    session: undefined,
  };
  const container = await render();
  const activity = container.querySelector<HTMLElement>(".agentplane-composer-model-activity");
  expect(activity?.textContent).toContain("Model activity: 8m ago");
  expect(activity?.textContent).not.toContain("Idle since");
});

it("sends the draft on Enter and clears it", async () => {
  const container = await render();
  const field = composer(container);
  await type(field, "hello");
  await press(field, {});
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "hello" } }]);
  expect(field.value).toBe("");
  const bubble = container.querySelector<HTMLElement>('.transcript-user-bubble[data-message-phase="local"]');
  expect(bubble?.querySelector(".transcript-verbatim")?.textContent).toBe("hello");
  expect(bubble?.parentElement?.querySelector('[role="status"] button')?.getAttribute("aria-label")).toBe(
    "Saved in browser · waiting for Dispatcher"
  );
  expect(bubble?.querySelector('[role="status"]')).toBeNull();
  expect(buttonIn(bubble?.parentElement, "Retry")).toBeDefined();
  expect(container.querySelector('[aria-label="Pending commands"]')).toBeNull();
});

it("queues a message through the Dispatcher for a fresh suspended Sandbox with wake-on-message", async () => {
  sandboxes = [changedSandbox(inventorySandbox("suspended", true), { observedState: { suspendedBy: "Requested" } })];
  sharedThread = {
    session: environmentDownSession(THREAD.metadata.threadId),
    summary: {
      feedStatus: FeedStatus.ENDED,
      attached: {
        harnessState: HarnessState.STOPPED,
        setupState: SetupState.SUCCEEDED,
        launchOutcome: LaunchOutcome.STARTED,
      },
    },
  };
  const sealedThread = threadState({ rows: [viewState({ harness: "stopped", status: ThreadFeedStatus.ENDED })] });
  const container = await render(sealedThread);
  const field = composer(container);
  expect(field.disabled).toBe(false);
  expect(button(container, "Send").disabled).toBe(true);
  expect(field.placeholder).toBe(
    "Sandbox is suspended (on request). Wake on message is on; submitted commands will queue while it wakes. Enter sends, Shift+Enter or Ctrl+Enter for a new line"
  );
  expect(container.querySelector('[role="status"]')).toBeNull();
  expect(buttonIn(container, "Resume Sandbox")).toBeUndefined();

  await type(field, "Continue after resume");
  await press(field, {});

  expect(field.value).toBe("");
  expect(vi.mocked(submitInput).mock.calls[0]?.[0]).toBe(THREAD.metadata.threadId);
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "Continue after resume" } }]);
});

it("disables the composer and offers resume for a fresh suspended Sandbox without wake-on-message", async () => {
  vi.mocked(resumeSandbox).mockResolvedValue(undefined);
  sandboxes = [changedSandbox(inventorySandbox("suspended", false), { observedState: { suspendedBy: "Requested" } })];
  sharedThread = {
    session: environmentDownSession(THREAD.metadata.threadId),
    summary: {
      feedStatus: FeedStatus.ENDED,
      attached: {
        harnessState: HarnessState.STOPPED,
        setupState: SetupState.SUCCEEDED,
        launchOutcome: LaunchOutcome.STARTED,
      },
    },
  };
  const sealedThread = threadState({ rows: [viewState({ harness: "stopped", status: ThreadFeedStatus.ENDED })] });
  const container = await render(sealedThread);
  const field = composer(container);
  expect(field.disabled).toBe(true);
  expect(field.placeholder).toBe(
    "Sandbox is suspended (on request). Wake on message is off; submitted commands stay queued until you resume it manually."
  );
  expect(container.querySelector('[role="status"]')).toBeNull();
  expect(container.querySelector('button[aria-label="Interrupt"]')).toBeNull();
  expect(container.querySelector('button[aria-label="Send"]')).toBeNull();
  const resume = buttonIn(container.querySelector(".agentplane-composer-controls"), "Resume Sandbox");
  expect(resume).toBeDefined();
  await act(async () => resume?.click());
  expect(resumeSandbox).toHaveBeenCalledExactlyOnceWith(
    expect.objectContaining({ name: THREAD.summary.sandbox!.name, uid: "20000000-0000-4000-8000-000000000001" })
  );
});

it.each([SetupState.FAILED, SetupState.INTERRUPTED])(
  "does not send input when the observed Session setup is %s",
  async (setupState) => {
    sandboxes = [inventorySandbox("ready", true)];
    sharedThread = {
      session: observedHostedSession(THREAD.metadata.threadId, {
        setup: setupState,
        harnessState: HarnessAttachment_State.DETACHED,
      }),
      summary: {
        feedStatus: FeedStatus.ENDED,
        attached: {
          harnessState: HarnessState.RUNNING,
          setupState: SetupState.SUCCEEDED,
          launchOutcome: LaunchOutcome.STARTED,
        },
      },
    };
    const sealedThread = threadState({ rows: [viewState({ harness: "stopped", status: ThreadFeedStatus.ENDED })] });
    const container = await render(sealedThread);
    expect(composer(container).disabled).toBe(true);
    await type(composer(container), "Do not send");
    await press(composer(container), {});
    expect(submitInput).not.toHaveBeenCalled();
  }
);

it.each<[string, Inventory, boolean]>([
  ["a stale suspended Sandbox", [inventorySandbox("suspended", true)], false],
  ["a missing Sandbox", [], true],
])("does not submit through %s inventory", async (_name, inventory, fresh) => {
  sandboxes = inventory;
  inventoryFresh = fresh;
  const container = await render();
  expect(composer(container).disabled).toBe(true);
  await type(composer(container), "Do not send");
  await press(composer(container), {});
  expect(submitInput).not.toHaveBeenCalled();
});

it("keeps an archived Thread read-only when its suspended Sandbox would wake on message", async () => {
  sandboxes = [inventorySandbox("suspended", true)];
  sharedThread = { metadata: { archived: true } };
  const container = await render();
  expect(composer(container).disabled).toBe(true);
  await type(composer(container), "Do not send");
  await press(composer(container), {});
  expect(submitInput).not.toHaveBeenCalled();
});

it("submits once for two Enters before the cleared draft renders, then takes the next draft", async () => {
  sandboxes = [inventorySandbox("suspended", true)];
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

it.each([
  [
    "a newly reserved Session",
    {
      summary: {
        feedStatus: FeedStatus.ACTIVE,
        attached: {
          harnessState: HarnessState.RUNNING,
          activeTurnId: "history-only-turn",
          launchOutcome: LaunchOutcome.PENDING,
        },
      },
      session: observedHostedSession(THREAD.metadata.threadId, { harnessState: HarnessAttachment_State.DETACHED }),
    },
  ],
  [
    "a Session whose harness exited",
    {
      summary: {
        feedStatus: FeedStatus.ENDED,
        attached: {
          harnessState: HarnessState.RUNNING,
          activeTurnId: "history-only-turn",
          launchOutcome: LaunchOutcome.STARTED,
        },
      },
      session: observedHostedSession(THREAD.metadata.threadId, { harnessState: HarnessAttachment_State.DETACHED }),
    },
  ],
] as const)("sends input to %s without an explicit resume action", async (_name, snapshot) => {
  sharedThread = snapshot;
  const dormant = threadState({ rows: [viewState({ harness: "stopped", status: ThreadFeedStatus.ENDED })] });
  const container = await render(dormant);
  expect(composer(container).disabled).toBe(false);
  expect(document.body.textContent).not.toContain("Resume harness");
  expect(container.querySelector(".agentplane-thread-status-indicator")?.getAttribute("aria-label")).toBe(
    "Harness not attached"
  );
  await type(composer(container), "Wake for this message");
  await press(composer(container), {});
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "Wake for this message" } }]);
  expect(vi.mocked(submitInput).mock.calls[0]?.[0]).toBe(THREAD.metadata.threadId);
});

it("queues input while one-shot setup runs", async () => {
  sharedThread = {
    summary: { attached: { setupState: SetupState.SUCCEEDED, harnessState: HarnessState.RUNNING } },
    session: observedHostedSession(THREAD.metadata.threadId, {
      setup: SetupState.RUNNING,
      harnessState: HarnessAttachment_State.DETACHED,
    }),
  };
  const container = await render(
    threadState({ rows: [viewState({ harness: "stopped", status: ThreadFeedStatus.ENDED })] })
  );
  expect(composer(container).disabled).toBe(false);
  await type(composer(container), "Continue after setup");
  await press(composer(container), {});
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "Continue after setup" } }]);
});

it.each([
  ["a failed Thread operation", threadState({ rows: [viewState({ status: ThreadFeedStatus.FAILED })] })],
  ["failed Thread synchronization", threadState({ windowError: "shape unavailable" })],
] as const)("keeps the composer read-only during %s", async (_label, state) => {
  const container = await render(state);
  expect(composer(container).disabled).toBe(true);
  await type(composer(container), "Do not submit");
  await press(composer(container), {});
  expect(submitInput).not.toHaveBeenCalled();
});

it("shows the thread id in the More menu, not inline once a name is set", async () => {
  const container = await render();
  expect(document.body.textContent).not.toContain(THREAD.metadata.threadId);
  await act(async () => button(container, "More").click());
  expect(document.body.textContent).toContain(THREAD.metadata.threadId);
});

// The topbar drops its Sandbox label when the name leaves no room, so the menu must carry it.
it("lists the thread's Sandbox and harness in the More menu", async () => {
  const container = await render();
  await act(async () => button(container, "More").click());
  const menu = document.querySelector('[role="menu"]');
  expect(menu?.textContent).toContain(THREAD.summary.sandbox!.name);
  expect(menu?.textContent).toContain("Claude");
});

it("reads the Sandbox for the topbar and the composer from the one inventory stream", async () => {
  sandboxes = [changedSandbox(inventorySandbox("suspended", true), { observedState: { suspendedBy: "Requested" } })];
  sharedThread = { session: environmentDownSession(THREAD.metadata.threadId) };
  const container = await render();
  const indicator = mounted.at(-1)?.topbarTitle?.querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("data-status")).toBe("suspended");
  expect(composer(container).placeholder).toMatch(/^Sandbox is suspended \(on request\)\./);
});

it.each<["Idle" | "Requested", string]>([
  ["Idle", "idle"],
  ["Requested", "on request"],
])("says a Sandbox suspended by %s was suspended %s, in the indicator and composer", async (suspendedBy, reason) => {
  sandboxes = [changedSandbox(inventorySandbox("suspended"), { observedState: { suspendedBy } })];
  sharedThread = { session: environmentDownSession(THREAD.metadata.threadId) };
  const container = await render();
  expect(container.querySelector(".agentplane-thread-status-indicator")?.getAttribute("aria-label")).toBe(
    "Environment down · Sandbox suspended"
  );
  expect(composer(container).placeholder).toContain(`Sandbox is suspended (${reason}). Wake on message is off;`);
  expect(container.querySelector('[role="status"]')).toBeNull();
});

it("offers to resume a suspended Sandbox, and only from a current inventory", async () => {
  vi.mocked(resumeSandbox).mockResolvedValue(undefined);
  sandboxes = [inventorySandbox("suspended")];
  const container = await render();
  const button = buttonIn(container, "Resume Sandbox");
  await act(async () => button?.click());
  expect(resumeSandbox).toHaveBeenCalledExactlyOnceWith(
    expect.objectContaining({ name: THREAD.summary.sandbox!.name, uid: "20000000-0000-4000-8000-000000000001" })
  );
});

it.each([
  ["a running Sandbox", [inventorySandbox()], true],
  ["a suspended Sandbox on a stale inventory", [inventorySandbox("suspended")], false],
])("does not offer to resume %s", async (_, inventory, fresh) => {
  sandboxes = inventory;
  inventoryFresh = fresh;
  expect(buttonIn(await render(), "Resume Sandbox")).toBeUndefined();
});

it("shows why a refused resume of a suspended Sandbox failed", async () => {
  vi.mocked(resumeSandbox).mockRejectedValue(new Error("the Sandbox is being deleted"));
  sandboxes = [inventorySandbox("suspended")];
  const container = await render();
  await act(async () => buttonIn(container, "Resume Sandbox")?.click());
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("the Sandbox is being deleted");
});

// A send to a suspended Sandbox wakes it: it is Running again with no Pod and no Ready condition until
// the controller has started the incarnation.
const waking: EnvironmentDescriptor = changedSandbox(autosuspendSandbox(null), {
  observedState: {
    phase: SandboxPhase.CREATING,
    readiness: create(ReadinessSchema, { state: ReadinessState.NOT_READY }),
  },
  kubernetesDiagnostics: { pod: undefined, sandboxStatus: {} },
});

it.each([
  ["a current", true, "Sandbox is starting. Submitted commands will queue until it is ready."],
  [
    "a stale",
    false,
    "Last observed Sandbox and Pod: Not started. Showing retained Thread history; controls are disabled.",
  ],
])("words a waking Sandbox on %s inventory without calling it a lasting state", async (_, fresh, notice) => {
  sandboxes = [waking];
  inventoryFresh = fresh;
  const container = await render();
  expect(container.querySelector('[role="status"]')?.textContent).toBe(notice);
  expect(container.querySelector(".agentplane-composer-autosuspend")?.textContent).toBe(
    fresh ? "Autosuspend waiting: Sandbox starting" : "Autosuspend timing unavailable"
  );
  expect(composer(container).disabled).toBe(!fresh);
});

it.each([null, "2026-01-01T12:12:00Z"])(
  "explains startup while the incarnation waits for runner readiness, with deadline %s",
  async (deadline) => {
    const starting = autosuspendSandbox(deadline);
    sandboxes = [
      changedSandbox(starting, {
        observedState: { readiness: create(ReadinessSchema, { state: ReadinessState.NOT_READY }) },
        kubernetesDiagnostics: {
          sandboxStatus: { conditions: [{ type: "Ready", status: "False", reason: "AwaitingEnvironmentRuntime" }] },
        },
      }),
    ];
    const hint = (await render()).querySelector(".agentplane-composer-autosuspend");
    expect(hint?.textContent).toBe("Autosuspend waiting: Sandbox starting");
    expect(hint?.getAttribute("title")).not.toContain("Next suspension attempt");
  }
);

it("does not call a failed Pod a Sandbox that is starting", async () => {
  const running = autosuspendSandbox(null);
  sandboxes = [
    changedSandbox(running, {
      observedState: { readiness: create(ReadinessSchema, { state: ReadinessState.NOT_READY }) },
      kubernetesDiagnostics: {
        sandboxStatus: {},
        pod: running.kubernetesDiagnostics?.pod && {
          ...running.kubernetesDiagnostics.pod,
          status: { phase: "Failed" },
        },
      },
    }),
  ];
  const container = await render();
  expect(container.querySelector('[role="status"]')?.textContent).toBe(
    "Last observed Sandbox and Pod: Failed. Showing retained Thread history; controls are disabled."
  );
  expect(container.querySelector(".agentplane-composer-autosuspend")?.textContent).toBe(
    "Autosuspend timing unavailable"
  );
});

it("shows the Dispatcher's active turn when History reports the runner stopped", async () => {
  threadRows = [
    threadView({
      metadata: THREAD.metadata,
      summary: {
        ...THREAD.summary,
        feedStatus: FeedStatus.ENDED,
        attached: { harnessState: HarnessState.STOPPED, activeTurnId: "" },
      },
      session: observedHostedSession(THREAD.metadata.threadId, { activeTurnId: "turn-1" }),
    }),
  ];
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("aria-label")).toBe("Turn running");
  expect(indicator?.getAttribute("data-status")).toBe("running");
});

it("does not infer Dispatcher activity from missing Sandbox inventory", async () => {
  sharedThread = { metadata: { archived: false } };
  sandboxes = [];
  const indicator = (
    await render(threadState({ rows: [viewState({ status: ThreadFeedStatus.FAILED })] }))
  ).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("aria-label")).toBe("Harness attached · ready for input");
  expect(indicator?.getAttribute("data-status")).toBe("idle");
});

const RUNNING = inventorySandbox();
const SUSPENDED = inventorySandbox("suspended");
const ABSENT = "Sandbox absent from last inventory snapshot. Current availability unknown";

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

  expect(eventStreamUrls.filter((url) => url === "/upstream/environments/v1/environments:watch")).toHaveLength(1);
});

it("mounts the thread from the live snapshot without fetching its metadata again", async () => {
  const container = await renderAfterThreadsSnapshot();

  expect(getThread).not.toHaveBeenCalled();
  expect(container.querySelector("textarea")).not.toBeNull();
});

it("fetches thread metadata when it is absent from the live snapshot", async () => {
  threadRows = [];

  const container = await render();

  expect(getThread).toHaveBeenCalledExactlyOnceWith(THREAD.metadata.threadId);
  expect(container.querySelector("textarea")).not.toBeNull();
});

it.each([
  ["failed", SessionStatus_Materialization.SESSION_MATERIALIZATION_FAILED],
  ["unknown", SessionStatus_Materialization.SESSION_MATERIALIZATION_UNKNOWN],
])("shows materialization %s without treating missing History as a deleted Sandbox", async (label, materialization) => {
  sharedThread = {
    summary: { sandbox: undefined, attached: undefined },
    session: {
      sessionId: THREAD.metadata.threadId,
      placement: {
        kind: {
          case: "centralSamplerV1",
          value: {
            executorEnvironment: { name: COMMAND_TARGET.sandbox, uid: COMMAND_TARGET.sandboxUid },
          },
        },
      },
      status: {
        materialization,
        reason: "invalid model",
      },
    },
  };
  const container = await renderAfterThreadsSnapshot();
  expect(container.textContent).toContain(`Session materialization ${label} · invalid model`);
  expect(container.textContent).not.toContain("Sandbox no longer exists");
  expect(container.textContent).toContain("composer-test · Central sampler");
  expect(container.querySelector("textarea")).toBeNull();
});

it.each([
  ["failed", SessionStatus_Materialization.SESSION_MATERIALIZATION_FAILED],
  ["unknown", SessionStatus_Materialization.SESSION_MATERIALIZATION_UNKNOWN],
])(
  "keeps retained History visible and the composer disabled after materialization %s",
  async (label, materialization) => {
    sharedThread = {
      summary: { projectionEpoch: "retained-epoch" },
      session: {
        sessionId: THREAD.metadata.threadId,
        status: {
          materialization,
          reason: "invalid model",
        },
      },
    };
    const container = await renderAfterThreadsSnapshot(
      threadState({ rows: [viewState(), testItem(1, ItemKind.TOOL_CALL, { toolName: "retained-tool" })] })
    );
    expect(container.textContent).toContain(`Session materialization ${label} · invalid model`);
    expect(container.textContent).toContain("retained-tool");
    expect(composer(container).disabled).toBe(true);
  }
);

it("does not infer deletion when no exact Sandbox identity is known", async () => {
  sharedThread = { summary: { sandbox: undefined, attached: undefined }, session: undefined };
  const container = await renderAfterThreadsSnapshot();
  expect(container.textContent).toContain("Sandbox identity unavailable");
  expect(container.textContent).not.toContain("Sandbox no longer exists");
  expect(composer(container).disabled).toBe(true);
});

// The Sandbox inventory controls whether commands can run or queue; Dispatcher activity owns the
// Session status and stays independent from stale or missing Sandbox inventory.
it.each<[string, Inventory, { fresh?: boolean; droppedFor?: number }, string | null]>([
  ["a running sandbox", [RUNNING], {}, null],
  ["an inventory not yet heard from", null, {}, null],
  ["a suspended sandbox", [SUSPENDED], {}, null],
  ["a deleted sandbox", [], {}, "Sandbox no longer exists."],
  ["a running sandbox on a stale inventory", [RUNNING], { fresh: false }, null],
  ["an absence from a stale inventory", [], { fresh: false }, ABSENT],
  ["a running sandbox on a stream that just dropped", [RUNNING], { droppedFor: 0 }, null],
  ["an absence from a stream that just dropped", [], { droppedFor: 0 }, ABSENT],
  ["an absence from a stream down past the grace", [], { droppedFor: DEGRADED_AFTER_MS }, ABSENT],
  ["a running sandbox on a stream down a minute", [RUNNING], { droppedFor: STALE_AFTER_MS }, null],
  ["a suspended sandbox on a stream down a minute", [SUSPENDED], { droppedFor: STALE_AFTER_MS }, "and Pod: Suspended."],
])("explains %s in the header", async (_, inventory, { fresh = true, droppedFor }, status) => {
  vi.useFakeTimers();
  sandboxes = inventory;
  inventoryFresh = fresh;
  inventoryDrops = droppedFor !== undefined;
  const container = await render();
  await act(async () => vi.advanceTimersByTime(droppedFor ?? 0));
  const texts = (role: string) => [...container.querySelectorAll(`[role="${role}"]`)].map((node) => node.textContent);
  expect(texts("status")).toEqual(status === null ? [] : [matching(status)]);
  expect(texts("alert")).toEqual([]);
});

it.each(["running", "dormant", "setup", "suspended", "waking"])(
  "queues an effort change with a %s harness or Sandbox without optimistically changing it",
  async (state) => {
    if (state === "suspended") sandboxes = [inventorySandbox("suspended", true)];
    if (state === "waking") sandboxes = [waking];
    if (state === "setup")
      sharedThread = {
        summary: { attached: { setupState: SetupState.SUCCEEDED, harnessState: HarnessState.RUNNING } },
        session: observedHostedSession(THREAD.metadata.threadId, {
          setup: SetupState.RUNNING,
          harnessState: HarnessAttachment_State.DETACHED,
        }),
      };
    const container = await render(
      state === "running"
        ? threadState()
        : threadState({ rows: [viewState({ harness: "stopped", status: ThreadFeedStatus.ENDED })] })
    );
    expect(container.querySelector<HTMLInputElement>('input[aria-label="Model"]')?.disabled).toBe(false);
    const picker = container.querySelector<HTMLInputElement>('input[aria-label="Reasoning effort"]');
    expect(picker?.disabled).toBe(false);
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
  }
);

it.each([
  [{ caughtUp: false }, "Catching up…"],
  [{ windowError: "test shape gone" }, "Model unavailable"],
  [{ rows: [viewState({ status: ThreadFeedStatus.FAILED, model: null })] }, "Model unavailable"],
  [{ rows: [viewState({ model: null })] }, "Model"],
])("names why %o shows no model: %s", async (state, placeholder) => {
  const picker = (await render(threadState(state))).querySelector<HTMLInputElement>('input[aria-label="Model"]');
  expect(picker?.placeholder).toBe(placeholder);
});

// A stopped window says so in its own alert; a stream merely reconnecting shows in the indicator only.
it.each<[{ reconnectingFor: number; windowError?: string }, string[]]>([
  [{ reconnectingFor: DEGRADED_AFTER_MS }, []],
  [{ reconnectingFor: STALE_AFTER_MS }, []],
  [{ reconnectingFor: STALE_AFTER_MS, windowError: "test shape gone" }, ["Thread synchronization stopped"]],
])("alerts only for a stopped window, never for a thread stream still reconnecting: %o", async (state, alerts) => {
  const container = await render(threadState(state));
  const shown = [...container.querySelectorAll('[role="alert"]')].map((node) => node.textContent);
  expect(shown).toEqual(alerts.map(matching));
});

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

async function admit(_threadId: string, commandId: string, text: string): Promise<CommandSubmission> {
  const command = create(CommandSchema, {
    commandId,
    operation: { case: "submitInput", value: { text } },
  });
  return {
    kind: "dispatch",
    dispatch: dispatchFromJson({
      commandId,
      sessionId: THREAD.metadata.threadId,
      ...COMMAND_TARGET,
      state: "DELIVERY_STATE_RUNNER_ADMITTED",
      admission: toJson(EventEntrySchema, admission(command)),
    }),
  };
}

function sentIds(): string[] {
  return vi.mocked(submitInput).mock.calls.map(([, commandId]) => commandId);
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
  const store = new LocalCommands(THREAD.metadata.threadId);
  const command = message("retained-admitted");
  // Older tabs saved bare admission evidence before commands returned a Dispatch.
  localStorage.setItem(
    `agentplane.pending:${encodeURIComponent(THREAD.metadata.threadId)}:${command.commandId}`,
    JSON.stringify({
      command,
      sessionAddressed: true,
      submittedAt: 1,
      admission: toJson(EventEntrySchema, admission(command)),
    })
  );
  // Its unadmitted sibling being sent shows the mount's delivery ran.
  store.remember(message("retained-unadmitted"));
  vi.mocked(submitInput).mockImplementation(admit);
  const container = await render();
  expect(sentIds()).toEqual(["retained-unadmitted"]);
  expect(progressLabel(container, "retained-admitted")).toBe("Runner accepted · waiting for agent confirmation");
  expect(
    [...pendingRow(container, "retained-admitted").querySelectorAll(".agentplane-command-light")].map((light) =>
      light.getAttribute("data-state")
    )
  ).toEqual(["done", "done", "done", "waiting"]);
});

it("keeps legacy input visible for reconciliation without replaying it on mount or reconnect", async () => {
  const threadId = THREAD.metadata.threadId;
  const legacy = message("legacy-targeted");
  localStorage.setItem(
    `agentplane.pending:${encodeURIComponent(threadId)}:${legacy.commandId}`,
    JSON.stringify({ command: legacy, target: COMMAND_TARGET, submittedAt: 1, admission: null })
  );
  new LocalCommands(threadId).remember(message("session-retry"));
  vi.mocked(submitInput).mockImplementation(admit);

  const container = await render();
  expect(sentIds()).toEqual(["session-retry"]);
  const row = pendingRow(container, legacy.commandId);
  expect(row.textContent).toContain("Test input legacy-targeted");
  expect(progressLabel(container, legacy.commandId)).toContain("Waiting for History evidence");
  expect(buttonIn(row, "Retry")).toBeUndefined();
  expect(buttonIn(row, "Cancel")).toBeUndefined();

  await act(async () => window.dispatchEvent(new Event("online")));
  expect(sentIds()).toEqual(["session-retry"]);
});

it.each([
  ["sandbox_resuming", "Sandbox is waking; message is queued"],
  ["wake_on_send_disabled", "Message is queued; wake on message is off, so resume the Sandbox manually to send it"],
  [
    'sandbox_wake_failed: sandboxes.agentplane.allegedly.works "box" is forbidden: cannot update resource "sandboxes"',
    'Dispatcher could not wake the Sandbox through Kubernetes and keeps retrying: sandboxes.agentplane.allegedly.works "box" is forbidden: cannot update resource "sandboxes"',
  ],
  [
    "runner_unreachable: Unavailable: lost runner reply",
    "Dispatcher could not reach the runner and keeps retrying: Unavailable: lost runner reply",
  ],
])("shows a queued Dispatcher receipt for %s without offering a retry", async (reason, expectedLabel) => {
  const queued = message("queued-by-dispatcher");
  new LocalCommands(THREAD.metadata.threadId).remember(queued);
  vi.mocked(submitInput).mockResolvedValue({
    kind: "dispatch",
    dispatch: dispatchFromJson({
      commandId: queued.commandId,
      sessionId: THREAD.metadata.threadId,
      ...COMMAND_TARGET,
      state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
      reason,
      updatedAt: "2026-10-14T12:00:00Z",
      admission: null,
    }),
  });
  const container = await render();
  const row = pendingRow(container, queued.commandId);
  expect(row.querySelector('[data-stage="queued"]')).not.toBeNull();
  expect(progressLabel(container, queued.commandId)).toBe(expectedLabel);
  expect(
    [...row.querySelectorAll(".agentplane-command-light")].map((light) => light.getAttribute("data-state"))
  ).toEqual(["done", "done", "waiting", "future"]);
  expect(buttonIn(row, "Retry")).toBeUndefined();
  expect(buttonIn(row, "Cancel")).toBeUndefined();
});

it("shows the cause when a send-triggered Sandbox wake misses its deadline", async () => {
  const failed = message("wake-deadline");
  new LocalCommands(THREAD.metadata.threadId).remember(failed);
  const reason = "wake_deadline_expired: Ready=False/Unschedulable: 0/2 nodes are available: PVC is not bound";
  const dispatch = dispatchFromJson({
    commandId: failed.commandId,
    sessionId: THREAD.metadata.threadId,
    ...COMMAND_TARGET,
    state: "DELIVERY_STATE_FAILED",
    reason,
    updatedAt: "2026-10-14T12:00:00Z",
    admission: null,
  });
  vi.mocked(submitInput).mockResolvedValue({ kind: "dispatch", dispatch });

  const container = await render();
  const row = pendingRow(container, failed.commandId);
  expect(row.querySelector('[data-stage="dispatch_failed"]')).not.toBeNull();
  expect(progressLabel(container, failed.commandId)).toBe(
    "Dispatcher could not deliver: Sandbox did not become Ready within 10 minutes: Ready=False/Unschedulable: 0/2 nodes are available: PVC is not bound"
  );
  expect(buttonIn(row, "Retry")).toBeUndefined();
  expect(buttonIn(row, "Cancel")).toBeUndefined();
});

function dispatchReceipt(commandId: string, state: string, reason: string, updatedAt: string) {
  return dispatchFromJson({
    commandId,
    sessionId: THREAD.metadata.threadId,
    ...COMMAND_TARGET,
    state,
    reason,
    updatedAt,
    admission: null,
  });
}

// A Dispatch as the watch carries it: proto-JSON leaves out what is unset, such as a reason or admission.
function dispatchRow(commandId: string, state: string, reason: string | undefined, updatedAt: string) {
  return { commandId, sessionId: THREAD.metadata.threadId, ...COMMAND_TARGET, state, reason, updatedAt };
}

async function openedWatch(): Promise<ServedWatch> {
  await vi.waitFor(() => expect(dispatchWatches).not.toHaveLength(0));
  return dispatchWatches[0];
}

// What the Dispatcher holds, then the frame that says the watch has it all.
async function sendDispatches(rows: object[]): Promise<void> {
  const watch = await openedWatch();
  await act(async () => {
    for (const [index, dispatch] of rows.entries()) watch.send({ dispatch, resumeToken: `page-dispatch-${index}` });
    watch.send({ caughtUp: {}, resumeToken: "page-dispatch-checkpoint" });
  });
}

it("replaces a queued Dispatcher receipt with the failure its stream reports", async () => {
  const waiting = message("queued-then-failed");
  new LocalCommands(THREAD.metadata.threadId).remember(waiting);
  const queued = dispatchReceipt(
    waiting.commandId,
    "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    "sandbox_not_ready",
    "2026-10-17T12:26:30Z"
  );
  vi.mocked(submitInput).mockResolvedValue({ kind: "dispatch", dispatch: queued });

  const container = await render();
  expect(progressLabel(container, waiting.commandId)).toBe("Sandbox is not ready; message is queued");
  await sendDispatches([
    dispatchRow(
      waiting.commandId,
      "DELIVERY_STATE_FAILED",
      "runner NotFound: no rollout found for thread id",
      "2026-10-17T12:26:35Z"
    ),
  ]);

  await vi.waitFor(() =>
    expect(pendingRow(container, waiting.commandId).querySelector('[data-stage="dispatch_failed"]')).not.toBeNull()
  );
});

it("follows the dispatches of queued commands only until they settle", async () => {
  const waiting = message("followed-until-admitted");
  new LocalCommands(THREAD.metadata.threadId).remember(waiting);
  const queued = dispatchReceipt(
    waiting.commandId,
    "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    "sandbox_not_ready",
    "2026-10-17T12:26:30Z"
  );
  vi.mocked(submitInput).mockResolvedValue({ kind: "dispatch", dispatch: queued });

  await render();
  const watch = await openedWatch();
  expect(watch.url.searchParams.getAll("filter.commandIds")).toEqual([waiting.commandId]);
  await sendDispatches([
    dispatchRow(waiting.commandId, "DELIVERY_STATE_RUNNER_ADMITTED", undefined, "2026-10-17T12:26:45Z"),
  ]);

  await vi.waitFor(() => expect(watch.signal?.aborted).toBe(true));
});

it("calls a command the Dispatcher holds no row for unknown, as after Sandbox deletion", async () => {
  const waiting = message("row-deleted-with-sandbox");
  new LocalCommands(THREAD.metadata.threadId).remember(waiting);
  vi.mocked(submitInput).mockResolvedValue({
    kind: "dispatch",
    dispatch: dispatchReceipt(
      waiting.commandId,
      "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
      "sandbox_not_ready",
      "2026-10-17T12:26:30Z"
    ),
  });

  const container = await render();
  await sendDispatches([]);

  await vi.waitFor(() =>
    expect(pendingRow(container, waiting.commandId).querySelector('[data-stage="unknown"]')).not.toBeNull()
  );
});

it("opens compact command detail on touch and closes on blur or Escape", async () => {
  new LocalCommands(THREAD.metadata.threadId).remember(message("touch"));
  const container = await render();
  const progress = pendingRow(container, "touch").querySelector<HTMLElement>(".agentplane-command-progress");
  const indicator = progress?.querySelector<HTMLButtonElement>(".agentplane-command-progress-hit");
  expect(indicator?.getAttribute("aria-label")).toBeTruthy();
  expect(progress?.querySelectorAll(".agentplane-command-light")).toHaveLength(4);
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
  new LocalCommands(THREAD.metadata.threadId).remember(message("unsent"));
  const container = await render();
  expect(vi.mocked(submitInput)).not.toHaveBeenCalled();
  const row = pendingRow(container, "unsent");
  expect(buttonIn(row, "Cancel")).toBeDefined();
  expect(buttonIn(row, "Retry")).toBeUndefined();
  await act(async () => buttonIn(row, "Cancel")?.click());
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toEqual([]);
  online.mockReturnValue(true);
  await act(async () => window.dispatchEvent(new Event("online")));
  expect(vi.mocked(submitInput)).not.toHaveBeenCalled();
});

it("does not offer Cancel once an offline command has begun its first HTTP attempt", async () => {
  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  new LocalCommands(THREAD.metadata.threadId).remember(message("queued"));
  const container = await render();
  expect(buttonIn(pendingRow(container, "queued"), "Cancel")).toBeDefined();
  online.mockReturnValue(true);
  await act(async () => window.dispatchEvent(new Event("online")));
  expect(sentIds()).toEqual(["queued"]);
  expect(new LocalCommands(THREAD.metadata.threadId).cancelUnsent("queued")).toBe(false);
  expect(buttonIn(pendingRow(container, "queued"), "Cancel")).toBeUndefined();
});

it("shows a delivery that outlives its deadline as unconfirmed and retriable", async () => {
  new LocalCommands(THREAD.metadata.threadId).remember(message("hung"));
  let expire!: (reason: unknown) => void;
  vi.mocked(submitInput)
    .mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        expire = reject;
      })
    )
    .mockImplementationOnce(admit);
  const container = await render();
  expect(progressLabel(container, "hung")).toContain("Saved in browser · waiting for Dispatcher");
  expect(
    [...pendingRow(container, "hung").querySelectorAll(".agentplane-command-light")].map((light) =>
      light.getAttribute("data-state")
    )
  ).toEqual(["done", "waiting", "future", "future"]);
  expect(pendingRow(container, "hung").querySelectorAll("[data-state=waiting]")).toHaveLength(1);

  await act(async () => expire(new DOMException("signal timed out", "TimeoutError")));
  expect(progressLabel(container, "hung")).toContain("Dispatcher receipt unconfirmed");
  expect(pendingRow(container, "hung").querySelector('[data-state="uncertain"]')).not.toBeNull();
  expect(progressLabel(container, "hung")).toContain("signal timed out");
  expect(sentIds()).toEqual(["hung"]);

  await act(async () => retry(container, "hung").click());
  expect(sentIds()).toEqual(["hung", "hung"]);
  expect(progressLabel(container, "hung")).toContain("Runner accepted · waiting for agent confirmation");
  expect(progressLabel(container, "hung")).not.toContain("signal timed out");
});

it("leaves a command whose delivery reply was lost unconfirmed", async () => {
  new LocalCommands(THREAD.metadata.threadId).remember(message("lost"));
  vi.mocked(submitInput).mockRejectedValueOnce(new Error("Command admission unconfirmed: 503"));
  const container = await render();
  expect(progressLabel(container, "lost")).toBe("Dispatcher receipt unconfirmed: Command admission unconfirmed: 503");
});

it("redelivers an unconfirmed command when the browser comes back online", async () => {
  new LocalCommands(THREAD.metadata.threadId).remember(message("offline"));
  vi.mocked(submitInput)
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
  new LocalCommands(THREAD.metadata.threadId).remember(
    create(CommandSchema, {
      commandId: "inline-model",
      operation: { case: "changeModel", value: { model: "next-model" } },
    })
  );
  const container = await render();
  const local = pendingRow(container, "inline-model");
  expect(local.closest('[aria-label="Thread history"]')).not.toBeNull();
  expect(local.querySelector('[data-stage="local"]')).not.toBeNull();

  const projected = entity({
    case: "command",
    value: {
      operation: Operation.CHANGE_MODEL,
      outcome: ThreadCommandOutcome.EFFECTED,
      outcomeCursor: 2n,
      outcomeReason: undefined,
      requestedValue: "next-model",
    },
  });
  projected.entityId = "inline-model";
  projected.cursor = 2n;
  await rerender(container, threadState({ rows: [viewState(), projected] }));
  expect(container.querySelectorAll('[data-command-id="inline-model"]')).toHaveLength(1);
  const settled = pendingRow(container, "inline-model");
  expect(settled).not.toBeNull();
  expect(settled.closest("[data-history-row-key]")).not.toBeNull();
  expect(settled.querySelector('[data-stage="local"]')).toBeNull();
  expect(settled.querySelector(".agentplane-command-check")).not.toBeNull();
  expect(settled.textContent).toContain("Change model to next-model");
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toHaveLength(0);
  online.mockRestore();
});

it("shows a server-only pending command as saved, not as a local delivery", async () => {
  const section = await renderServerOnlyChangeModelSection({
    outcome: ThreadCommandOutcome.PENDING,
    outcomeCursor: undefined,
    outcomeReason: undefined,
    requestedValue: "next-model",
  });
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
  const section = await renderServerOnlyChangeModelSection({
    outcome: ThreadCommandOutcome.FAILED,
    outcomeCursor: 1n,
    outcomeReason: "model unavailable",
    requestedValue: undefined,
  });
  const pending = pendingRow(section, "test-entity");
  expect(pending.querySelector(".agentplane-command-progress-hit")?.getAttribute("aria-label")).toBe(
    "Failed: model unavailable"
  );
  expect(pending.querySelector('[data-stage="failed"] [data-state="failed"]')).not.toBeNull();
});

it("renders a server-only no-op control as a settled label, not a progress light", async () => {
  const section = await renderServerOnlyChangeModelSection({
    outcome: ThreadCommandOutcome.NOOP,
    outcomeCursor: 1n,
    outcomeReason: "model already selected",
    requestedValue: "current-model",
  });
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
        entity({
          case: "command",
          value: {
            operation: Operation.SUBMIT_INPUT,
            outcome:
              outcome === "failed"
                ? ThreadCommandOutcome.FAILED
                : outcome === "noop"
                  ? ThreadCommandOutcome.NOOP
                  : ThreadCommandOutcome.EFFECTED,
            outcomeCursor: 2n,
            outcomeReason: outcome === "effected" ? undefined : status.split(": ")[1],
            requestedValue: undefined,
            inputRef: reference("failed-input", ThreadPayloadField.COMMAND_INPUT),
          },
        }),
      ],
    })
  );
  const bubble = container.querySelector<HTMLElement>(`.transcript-user-bubble[data-message-phase="${phase}"]`);
  const plainOutcome = bubble?.parentElement?.querySelector(".agentplane-command-noop");
  expect(plainOutcome?.textContent).toBe(outcome === "noop" ? status : undefined);
  expect(plainOutcome?.getAttribute("title")).toBe(outcome === "noop" ? status : undefined);
  expect(bubble?.parentElement?.querySelector(".agentplane-command-progress") === null).toBe(outcome === "noop");
  expect(bubble?.parentElement?.querySelectorAll(".agentplane-command-light")).toHaveLength(
    outcome === "effected" || outcome === "noop" ? 0 : 4
  );
  expect(bubble?.parentElement?.querySelectorAll(".agentplane-command-check")).toHaveLength(
    outcome === "effected" ? 1 : 0
  );
  expect(bubble?.parentElement?.querySelector(".agentplane-command-progress-hit")?.getAttribute("aria-label")).toBe(
    outcome === "noop" ? undefined : status
  );
  expect(bubble?.parentElement?.querySelector("button[aria-label='Dismiss']")).toBeNull();
  expect(container.querySelector(`.transcript-user-bubble[data-message-phase="${phase}"]`)).not.toBeNull();
});

it("keeps a still-pending sent message out of the pending-commands box, since it renders inline instead", async () => {
  const container = await render(
    threadState({
      rows: [
        viewState(),
        entity({
          case: "command",
          value: {
            operation: Operation.SUBMIT_INPUT,
            outcome: ThreadCommandOutcome.PENDING,
            outcomeCursor: undefined,
            outcomeReason: undefined,
            requestedValue: undefined,
            inputRef: reference("test-message", ThreadPayloadField.COMMAND_INPUT),
          },
        }),
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

async function renderServerOnlyChangeModelSection(value: {
  outcome: ThreadCommandOutcome;
  outcomeCursor: bigint | undefined;
  outcomeReason: string | undefined;
  requestedValue: string | undefined;
}): Promise<HTMLElement> {
  const [section] = await renderHistory(
    [
      entity({
        case: "command",
        value: { operation: Operation.CHANGE_MODEL, ...value },
      }),
    ],
    false
  );
  return section;
}

function pendingInput(id: string, cursor: string): ThreadEntity {
  const row = entity({
    case: "command",
    value: {
      operation: Operation.SUBMIT_INPUT,
      outcome: ThreadCommandOutcome.PENDING,
      outcomeCursor: undefined,
      outcomeReason: undefined,
      requestedValue: undefined,
      inputRef: reference(id, ThreadPayloadField.COMMAND_INPUT),
    },
  });
  row.entityId = id;
  row.cursor = BigInt(cursor);
  row.revisionCursor = BigInt(cursor);
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

  expect(local.querySelector(".transcript-verbatim")?.textContent).toBe("Text submitted by this browser.");
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

  expect(section.querySelector(".transcript-verbatim")?.textContent).toBe("Text returned by the service.");
  expect(onInputEchoLoaded).toHaveBeenCalledWith("local-input");
});

it.each(["Bash", "commandExecution"])(
  "does not mark a failed %s shell call as a failed compacted run",
  async (toolName) => {
    const [run] = await renderHistory(
      [
        testItem(1, ItemKind.TOOL_CALL, {
          toolName: toolName,
          completion: { case: "tool", value: { succeeded: false } },
        }),
        testItem(2, ItemKind.REASONING, {}, { textRef: reference("entity-2", ThreadPayloadField.TEXT) }),
      ],
      false,
      { "entity-2:text": "A command may exit nonzero as part of normal investigation." }
    );

    const summary = run.querySelector(".ui-disclosure-summary")!;
    expect(summary.querySelector('[role="img"][aria-label="Failed"]')).toBeNull();
  }
);

it("folds a run of tool calls and reasoning behind its summary until it is opened", async () => {
  const [run] = await renderHistory(
    [
      testItem(1, ItemKind.TOOL_CALL, {
        toolName: "test-read",
        completion: { case: "tool", value: { succeeded: true } },
      }),
      testItem(2, ItemKind.REASONING, {}, { textRef: reference("test-entity-2", ThreadPayloadField.TEXT) }),
      testItem(3, ItemKind.TOOL_CALL, {
        toolName: "test-shell",
        completion: { case: "tool", value: { succeeded: false } },
      }),
    ],
    false,
    { "test-entity-2:text": "The plan is to **inspect** the evidence." }
  );
  const summary = run.querySelector(".ui-disclosure-summary")!;
  expect(summary.textContent).toContain("2 tool calls, 1 reasoning step");
  expect(summary.querySelector('[role="img"][aria-label="Failed"]')).not.toBeNull();
  expect(run.textContent).not.toContain("test-read");

  await toggle(summary);
  expect(run.textContent).toContain("test-read");
  expect(run.textContent).toContain("test-shell");
  expect(run.textContent).toContain("The plan is to inspect the evidence.");
  expect(run.querySelector(".transcript-step-preview strong")?.textContent).toBe("inspect");
  // Each step keeps its own evidence; the run is not an entity and has none.
  expect(run.querySelectorAll('button[aria-label="Evidence"]')).toHaveLength(3);
});

it("marks an unfinished run as streaming in the live turn, and as incomplete once that is over", async () => {
  const segments = [
    testItem(1, ItemKind.REASONING, { completion: { case: undefined } }),
    testItem(2, ItemKind.TOOL_CALL),
  ];
  const [live] = await renderHistory(segments, true);
  expect(live.querySelector('.ui-disclosure-summary [role="img"]')?.getAttribute("aria-label")).toBe("Streaming");
  const [retained] = await renderHistory(segments, false);
  expect(retained.querySelector('.ui-disclosure-summary [role="img"]')?.getAttribute("aria-label")).toBe("Incomplete");
});

it("puts a live assistant-text cursor inline after its Markdown body", async () => {
  const body = "The answer is still being written.";
  const [row] = await renderHistory(
    [
      testItem(
        1,
        ItemKind.ASSISTANT_TEXT,
        { completion: { case: undefined } },
        { textRef: reference("streaming-reply", ThreadPayloadField.TEXT) }
      ),
    ],
    true,
    { "streaming-reply:text": body }
  );
  const cursor = row.querySelector<HTMLElement>(".ui-streaming-cursor");

  expect(cursor?.getAttribute("aria-label")).toBe("Streaming");
  expect(cursor?.getAttribute("data-character")).toBe(STREAMING_CURSOR);
  expect(cursor?.closest(".ui-markdown")).not.toBeNull();
  expect(cursor?.parentElement?.textContent?.trimEnd()).toBe(body);
});

it("shows a lone tool call directly and opens its details with one click", async () => {
  const [row] = await renderHistory(
    [
      testItem(
        1,
        ItemKind.TOOL_CALL,
        { toolName: "test-read", completion: { case: "tool", value: { succeeded: true } } },
        {
          argumentsRef: reference("single-tool", ThreadPayloadField.ARGUMENTS),
        }
      ),
    ],
    false,
    { "single-tool:arguments": '{"path":"test-file.txt"}' }
  );
  expect(row.querySelector(".transcript-step-title")?.textContent).toBe("test-read");
  expect(row.querySelectorAll(".ui-disclosure-summary")).toHaveLength(1);
  const summary = row.querySelector(".ui-disclosure-summary")!;
  await toggle(summary);
  expect(summary.getAttribute("aria-expanded")).toBe("true");
  expect(row.textContent).toContain("test-file.txt");
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
            { completion: { case: undefined }, recovery },
            { textRef: reference("recovered-reply", ThreadPayloadField.TEXT) }
          ),
        ],
        true,
        { "recovered-reply:text": "Interrupted reply" }
      );
      expect(row.querySelector(".ui-streaming-cursor")).toBeNull();
      expect(row.querySelector('[aria-label="Interrupted"]')).not.toBeNull();
    }
  );
});
