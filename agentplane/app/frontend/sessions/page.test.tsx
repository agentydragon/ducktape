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
  ListSessionDispatchesResponseSchema,
  DeliveryState,
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
  getDispatch,
  listSessionDispatches,
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
import { useDispatchWorkset } from "./thread_delivery";
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
  getDispatch: vi.fn(),
  listSessionDispatches: vi.fn(),
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
let sendWorksetOnOpen: boolean;
let threadRows: ThreadView[] | null = null;
let noticeInboxes: Array<{ inboxId: string; sessionId: string; queued: number; unacknowledged: number }> = [];
let deliveryRequest: (url: URL, init?: RequestInit) => Response | Promise<Response>;
let selectedReceipts: Array<Record<string, unknown> & { commandId: string; legacyDeletion?: boolean }>;
let receiptBatchRequests: URL[];

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
  sendWorksetOnOpen = true;
  threadRows = null;
  noticeInboxes = [];
  deliveryRequest = () => Response.json({ message: "route unavailable" }, { status: 404 });
  selectedReceipts = [];
  receiptBatchRequests = [];
  vi.spyOn(appConfigClient, "getFrontendConfig").mockImplementation(async () =>
    create(FrontendConfigSchema, {
      traceSearch: traceSearchSettings ?? undefined,
      langfuseSearch: langfuseSearchSettings ?? undefined,
    })
  );
  vi.mocked(getDispatch).mockResolvedValue(null);
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
    const url = new URL(String(input), window.location.href);
    if (
      url.pathname.includes("/notice-acknowledgements") ||
      /^\/upstream\/dispatcher\/v1\/sessions\/[^/]+\/dispatches$/.test(url.pathname)
    ) {
      return Promise.resolve(deliveryRequest(url, init));
    }
    if (url.pathname === `/upstream/dispatcher/v1/sessions/${THREAD.metadata.threadId}/dispatches:batchGet`) {
      receiptBatchRequests.push(url);
      const results = url.searchParams.getAll("commandIds").map((commandId) => {
        const found = selectedReceipts.find((row) => row.commandId === commandId);
        if (!found) return { commandId, notFound: {} };
        const { legacyDeletion, ...row } = found;
        const receipt = { ...row, sessionId: THREAD.metadata.threadId };
        return legacyDeletion ? { commandId, legacyDeletionReceipt: receipt } : { commandId, dispatch: receipt };
      });
      return Promise.resolve(Response.json({ results }));
    }
    if (new Headers(init?.headers).get("Accept") === "application/x-ndjson") {
      const worksetSession = url.searchParams.get("worksetSessionId");
      if (url.pathname === "/upstream/dispatcher/v1/dispatches:watch" && worksetSession) {
        const stream = new ServedWatch(String(input), init?.signal ?? undefined);
        dispatchWatches.push(stream);
        if (sendWorksetOnOpen) stream.send({ workset: { sessionId: worksetSession }, resumeToken: "test-workset" });
        return Promise.resolve(stream.response);
      }
      if (
        url.pathname === "/upstream/dispatcher/v1/sessions:watch" ||
        url.pathname === "/upstream/app/v1/threads:watch" ||
        url.pathname === "/upstream/history/v1/session-summaries:watch"
      ) {
        const stream = new ServedWatch(String(input), init?.signal ?? undefined);
        if (url.pathname === "/upstream/dispatcher/v1/sessions:watch") {
          for (const [index, session] of dispatcherSnapshotSessions().entries())
            stream.send({ session: toJson(SessionSchema, session), resumeToken: `test-session-${index}` });
          stream.send({ caughtUp: {}, resumeToken: "test-snapshot-token" });
        } else if (url.pathname === "/upstream/app/v1/threads:watch") {
          stream.send({ changed: {}, updatesConnected: true });
        } else {
          const ids = url.searchParams.getAll("selected.session_ids");
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
  mountKey?: string,
  threadId = THREAD.metadata.threadId
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
          <ThreadPage key={mountKey} threadId={threadId} />
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

function message(commandId: string, text = "Test input " + commandId): Command {
  return create(CommandSchema, {
    commandId,
    operation: { case: "submitInput", value: { text } },
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
  const progress = pendingRow(container, commandId).querySelector(".agentplane-command-progress");
  return (
    progress?.querySelector(".agentplane-command-progress-hit")?.getAttribute("aria-label") ?? progress?.textContent
  );
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
  expect(progressLabel(container, legacy.commandId)).toContain("Dispatcher could not verify");
  expect(buttonIn(row, "Check status")).toBeDefined();
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
  expect(row.querySelector('[role="alert"]')?.textContent).toBe(
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
  return vi.waitFor(() => {
    const watch = dispatchWatches.find(({ url }) => url.searchParams.has("filter.commandIds"));
    if (!watch) throw new Error("No command-ID Dispatcher watch");
    return watch;
  });
}

// What the Dispatcher holds, then the frame that says the watch has it all.
async function sendDispatches(rows: object[]): Promise<void> {
  const watch = await openedWatch();
  await act(async () => {
    for (const [index, dispatch] of rows.entries()) watch.send({ dispatch, resumeToken: `page-dispatch-${index}` });
    watch.send({ caughtUp: {}, resumeToken: "page-dispatch-checkpoint" });
  });
}

function selectedWorksetWatch(previous?: ServedWatch): Promise<ServedWatch> {
  return vi.waitFor(() => {
    const watch = dispatchWatches.find(
      ({ url, signal }) => url.searchParams.get("worksetSessionId") === THREAD.metadata.threadId && !signal?.aborted
    );
    if (!watch || watch === previous) throw new Error("No Session workset watch");
    return watch;
  });
}

it("shows Dispatcher delivery before History materializes and keeps one watch when History arrives", async () => {
  const container = await render({ window: null, error: null });
  const watch = await selectedWorksetWatch();
  await act(async () =>
    watch.send({
      workset: {
        sessionId: THREAD.metadata.threadId,
        active: [
          {
            commandId: "pre-history",
            operation: "OPERATION_SUBMIT_INPUT",
            state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
          },
        ],
      },
      resumeToken: "pre-history",
    })
  );
  expect(container.textContent).toContain("Loading thread…");
  expect(container.textContent).toContain("Queued in Dispatcher · waiting for runner");
  await rerender(container, threadState());
  expect(container.textContent).toContain("Queued in Dispatcher · waiting for runner");
  expect(dispatchWatches.filter(({ url }) => url.searchParams.has("worksetSessionId"))).toEqual([watch]);
});

it("discards the old workset when a Session becomes unavailable and is selected again", async () => {
  function WorksetProbe({ sessionId }: { sessionId: string | null }) {
    const view = useDispatchWorkset(sessionId);
    return <span>{view.snapshot?.recentIssues[0]?.reason ?? "no current workset"}</span>;
  }
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  const select = (sessionId: string | null) => act(async () => root.render(<WorksetProbe sessionId={sessionId} />));
  await select(THREAD.metadata.threadId);
  const first = await selectedWorksetWatch();
  await showIssues(first, issueReceipt("same-session", "1", "First workset"));
  expect(container.textContent).toBe("First workset");

  await select(null);
  expect(first.signal?.aborted).toBe(true);
  expect(container.textContent).toBe("no current workset");
  sendWorksetOnOpen = false;
  await select(THREAD.metadata.threadId);
  const next = await selectedWorksetWatch(first);
  expect(next.url.searchParams.has("resumeToken")).toBe(false);
  expect(container.textContent).toBe("no current workset");
  await showIssues(next, issueReceipt("same-session", "2", "Second workset"));
  expect(container.textContent).toBe("Second workset");
});

it("shows server delivery for a fresh browser and defers to settled History", async () => {
  const settled = entity({
    case: "command",
    value: {
      operation: Operation.CHANGE_MODEL,
      outcome: ThreadCommandOutcome.EFFECTED,
      outcomeCursor: 2n,
      outcomeReason: undefined,
      requestedValue: "test-model",
    },
  });
  settled.entityId = "settled-model";
  settled.cursor = 2n;
  const container = await render(threadState({ rows: [viewState(), settled] }));
  const watch = await selectedWorksetWatch();
  expect(watch.url.searchParams.has("filter.sessionId")).toBe(false);
  await act(async () => {
    watch.send({
      workset: {
        sessionId: THREAD.metadata.threadId,
        active: [
          {
            commandId: "remote-message",
            operation: "OPERATION_SUBMIT_INPUT",
            state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
          },
          {
            commandId: "remote-interrupt",
            operation: "OPERATION_INTERRUPT_TURN",
            state: "DELIVERY_STATE_DELIVERING_TO_RUNNER",
          },
        ],
        recentIssues: [
          {
            commandId: "remote-model",
            operation: "OPERATION_CHANGE_MODEL",
            state: "DELIVERY_STATE_UNKNOWN",
            journalAdmitted: true,
            reason: "Runner closed before confirming the effect",
          },
          {
            commandId: settled.entityId,
            operation: "OPERATION_CHANGE_MODEL",
            state: "DELIVERY_STATE_UNKNOWN",
            reason: "Intermediate delivery receipt",
          },
        ],
        recentAdmittedOrCancelled: [
          {
            commandId: "remote-effort",
            operation: "OPERATION_CHANGE_REASONING_EFFORT",
            state: "DELIVERY_STATE_RUNNER_ADMITTED",
          },
        ],
        moreActiveAvailable: true,
        moreRecentIssuesAvailable: true,
        moreRecentAdmittedOrCancelledAvailable: true,
      },
      resumeToken: "initial-workset",
    });
  });
  expect(container.querySelector('[aria-label="Dispatcher delivery"]')).toBeNull();
  for (const [id, status] of [
    ["remote-message", "queued"],
    ["remote-interrupt", "delivering"],
    ["remote-model", "unknown"],
  ]) {
    expect(container.querySelectorAll(`[data-command-id="${id}"]`)).toHaveLength(1);
    expect(pendingRow(container, id).querySelector(`[data-stage="${status}"]`)).not.toBeNull();
  }
  expect(container.textContent).toContain("Runner accepted the command");
  expect(container.querySelector('[data-command-id="remote-effort"]')).toBeNull();
  expect(container.textContent).not.toContain("Intermediate delivery receipt");
  expect(buttonIn(container, "Older delivery issues")).toBeDefined();
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toHaveLength(0);
});

it("keeps one History bubble for an unresolved receipt across dismissal and a newer revision", async () => {
  deliveryRequest = (url) => Response.json(url.pathname.endsWith(":batchGet") ? { acknowledgements: [] } : {});
  const input = entity({
    case: "command",
    value: { operation: Operation.SUBMIT_INPUT, outcome: ThreadCommandOutcome.PENDING },
  });
  input.entityId = "projected-unknown";
  const container = await render({ window: null, error: null });
  const watch = await selectedWorksetWatch();
  const issue = {
    ...issueReceipt(input.entityId, "32", "Runner outcome could not be confirmed"),
    operation: "OPERATION_SUBMIT_INPUT",
    state: "DELIVERY_STATE_UNKNOWN",
    journalAdmitted: true,
  };
  await showIssues(watch, issue);
  expect(pendingRow(container, input.entityId).textContent).toContain(issue.reason);
  await rerender(container, threadState({ rows: [viewState(), input] }));
  const rows = () => container.querySelectorAll(`[data-command-id="${input.entityId}"]`);
  expect(rows()).toHaveLength(1);
  expect(rows()[0].textContent).toContain(issue.reason);
  await act(async () => buttonIn(rows()[0], "Dismiss")!.click());
  expect(rows()).toHaveLength(1);
  expect(rows()[0].textContent).not.toContain(issue.reason);
  await showIssues(watch, { ...issue, receiptVersion: "33", reason: "A later uncertain outcome" });
  expect(rows()).toHaveLength(1);
  expect(rows()[0].textContent).toContain("A later uncertain outcome");
});

it("shows the Dispatcher input when an unspecified History command has no rendered bubble", async () => {
  const command = entity({
    case: "command",
    value: { operation: Operation.SUBMIT_INPUT, outcome: ThreadCommandOutcome.UNSPECIFIED },
  });
  command.entityId = "unprojected-input";
  const container = await render(threadState({ rows: [viewState(), command] }));
  const watch = await selectedWorksetWatch();
  await showIssues(watch, {
    ...issueReceipt(command.entityId, "12"),
    operation: "OPERATION_SUBMIT_INPUT",
  });
  expect(container.querySelectorAll(`[data-command-id="${command.entityId}"]`)).toHaveLength(1);
});

it("leaves a locally retained command with its existing browser row", async () => {
  const local = message("local-workset");
  new LocalCommands(THREAD.metadata.threadId).remember(local);
  const container = await render();
  const watch = await selectedWorksetWatch();
  await act(async () => {
    watch.send({
      workset: {
        sessionId: THREAD.metadata.threadId,
        active: [
          {
            commandId: local.commandId,
            operation: "OPERATION_SUBMIT_INPUT",
            state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
          },
        ],
      },
      resumeToken: "matching",
    });
  });
  expect(pendingRow(container, local.commandId)).not.toBeNull();
  expect(container.querySelectorAll(`[data-command-id="${local.commandId}"]`)).toHaveLength(1);
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toHaveLength(1);
});

const ISSUE_TIMESTAMP = "2026-11-20T12:00:00.123456Z";

function issueReceipt(commandId: string, receiptVersion: string, reason = "Delivery refused") {
  return {
    commandId,
    receiptVersion,
    reason,
    operation: "OPERATION_SUBMIT_INPUT",
    state: "DELIVERY_STATE_FAILED",
    updatedAt: ISSUE_TIMESTAMP,
  };
}

async function showIssues(watch: ServedWatch, ...recentIssues: Array<ReturnType<typeof issueReceipt>>) {
  await act(async () => {
    watch.send({ workset: { sessionId: THREAD.metadata.threadId, recentIssues }, resumeToken: "delivery-issues" });
  });
}

it("acknowledges only the exact revision, including a matching local notice, and preserves the local command", async () => {
  const local = message("local-acknowledged");
  const first = issueReceipt(local.commandId, "9007199254740993");
  const newer = issueReceipt(local.commandId, "9007199254740994", "Dispatcher found a new problem");
  selectedReceipts = [first];
  const store = new LocalCommands(THREAD.metadata.threadId);
  store.remember(local);
  store.updateDispatch(local.commandId, dispatchFromJson({ ...first, sessionId: THREAD.metadata.threadId }));
  const writes: unknown[] = [];
  deliveryRequest = (url, init) => {
    if (url.pathname.endsWith(":batchGet")) return Response.json({ acknowledgements: [] });
    writes.push(JSON.parse(String(init?.body)));
    return Response.json({});
  };
  const container = await render();
  const watch = await selectedWorksetWatch();
  await showIssues(watch, first);
  const localRow = pendingRow(container, local.commandId);
  expect(localRow).not.toBeNull();
  expect(container.querySelectorAll(`[data-command-id="${local.commandId}"]`)).toHaveLength(1);
  await act(async () => buttonIn(localRow!, "Dismiss")!.click());
  expect(writes).toEqual([{ notice: { commandId: local.commandId, receiptVersion: "9007199254740993" } }]);
  expect(container.querySelector(`[data-command-id="${local.commandId}"]`)).toBeNull();
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toHaveLength(1);
  await showIssues(watch);
  expect(container.querySelector(`[data-command-id="${local.commandId}"]`)).toBeNull();
  expect(receiptBatchRequests.at(-1)?.searchParams.getAll("commandIds")).toEqual([local.commandId]);
  selectedReceipts = [newer];
  await showIssues(watch, newer);
  expect(container.textContent).toContain("Dispatcher found a new problem");
});

it("reconciles an old synthetic missing receipt by exact ID and original content", async () => {
  const local = message("old-deletion", ".");
  const unrelated = message("other-old-deletion", ".");
  for (const command of [local, unrelated]) {
    const store = new LocalCommands(THREAD.metadata.threadId);
    store.remember(command);
    store.updateDispatch(
      command.commandId,
      dispatchFromJson({
        commandId: command.commandId,
        sessionId: THREAD.metadata.threadId,
        state: "DELIVERY_STATE_UNKNOWN",
        reason: "sandbox_deleted",
      })
    );
  }
  const exact = {
    ...issueReceipt(local.commandId, "91", "Runner could not apply this operation"),
    operation: "OPERATION_SUBMIT_INPUT",
    state: "DELIVERY_STATE_UNKNOWN",
  };
  selectedReceipts = [{ ...exact, command: toJson(CommandSchema, local) }];
  const writes: unknown[] = [];
  deliveryRequest = (url, init) => {
    if (url.pathname.endsWith(":batchGet")) return Response.json({ acknowledgements: [] });
    writes.push(JSON.parse(String(init?.body)));
    return Response.json({});
  };
  const container = await render();
  const watch = await selectedWorksetWatch();
  await showIssues(watch, exact);
  expect(container.querySelectorAll(`[data-command-id="${local.commandId}"]`)).toHaveLength(1);
  const bubble = pendingRow(container, local.commandId);
  expect(bubble.textContent).toContain(exact.reason);
  await act(async () => buttonIn(bubble, "Dismiss")!.click());
  expect(writes).toEqual([{ notice: { commandId: local.commandId, receiptVersion: "91" } }]);
  expect(container.querySelector(`[data-command-id="${local.commandId}"]`)).toBeNull();
  const unmatched = pendingRow(container, unrelated.commandId);
  expect(unmatched.textContent).toContain("Dispatcher did not return a receipt for this command");
  expect(buttonIn(unmatched, "Dismiss")).toBeUndefined();
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toHaveLength(2);
});

it("keeps a saved local failure with no authoritative receipt for acknowledgement", async () => {
  const local = message("unverified-saved-issue");
  const receipt = issueReceipt(local.commandId, "51", "Saved local failure");
  const store = new LocalCommands(THREAD.metadata.threadId);
  store.remember(local);
  store.updateDispatch(local.commandId, dispatchFromJson({ ...receipt, sessionId: THREAD.metadata.threadId }));
  const request = vi.fn(() => Response.json({ acknowledgements: [{ notice: receipt }] }));
  deliveryRequest = request;
  const container = await render();
  const row = pendingRow(container, local.commandId);
  expect(row?.textContent).toContain(receipt.reason);
  expect(buttonIn(row, "Dismiss")).toBeUndefined();
  expect(request).not.toHaveBeenCalled();
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toHaveLength(1);
});

it("bounds exact lookup and shared acknowledgement batches when a browser has over 100 saved issues", async () => {
  const store = new LocalCommands(THREAD.metadata.threadId);
  for (let index = 1; index <= 105; index++) {
    const command = message(`saved-issue-${index}`);
    const receipt = issueReceipt(command.commandId, String(index));
    selectedReceipts.push(receipt);
    store.remember(command);
    store.updateDispatch(command.commandId, dispatchFromJson({ ...receipt, sessionId: THREAD.metadata.threadId }));
  }
  const appBatchSizes: number[] = [];
  deliveryRequest = (_url, init) => {
    const { notices } = JSON.parse(String(init?.body));
    appBatchSizes.push(notices.length);
    return Response.json({ acknowledgements: notices.map((notice: object) => ({ notice })) });
  };
  const container = await render();
  const largeBatches = receiptBatchRequests.map((url) => url.searchParams.getAll("commandIds").length);
  expect(largeBatches).toEqual([16, 16, 16, 16, 16, 16, 9]);
  expect(appBatchSizes).toEqual([100, 5]);
  expect(container.querySelector('[data-command-id="saved-issue-105"]')).toBeNull();
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toHaveLength(105);
});

it("keeps unseen delivery issues visible when acknowledgement reads or writes fail", async () => {
  deliveryRequest = () => Response.json({ message: "temporarily unavailable" }, { status: 503 });
  const container = await render();
  const watch = await selectedWorksetWatch();
  await showIssues(watch, issueReceipt("keep-unseen", "41"));
  const row = pendingRow(container, "keep-unseen");
  expect(row.textContent).toContain("Delivery refused");
  expect(container.textContent).toContain("Could not load acknowledgements.");
  await act(async () => buttonIn(row, "Dismiss")!.click());
  expect(row.textContent).toContain("Delivery refused");
  expect(container.textContent).toContain("Could not acknowledge the delivery issue.");
});

it.each(["conversation", "archive"])("shares an exact acknowledgement from the %s across both views", async (from) => {
  const issue = issueReceipt("shared-issue", "41", "Current delivery failure");
  vi.mocked(listSessionDispatches).mockResolvedValue(
    create(ListSessionDispatchesResponseSchema, {
      dispatches: [dispatchFromJson({ ...issue, sessionId: THREAD.metadata.threadId })],
    })
  );
  const writes = vi.fn();
  const lookups = vi.fn();
  let finishLookup!: (response: Response) => void;
  const olderResponse = new Promise<Response>((resolve) => {
    finishLookup = resolve;
  });
  deliveryRequest = (url, init) => {
    const body = JSON.parse(String(init?.body));
    if (url.pathname.endsWith(":batchGet")) {
      lookups(body.notices);
      return lookups.mock.calls.length === 1 ? olderResponse : Response.json({ acknowledgements: [] });
    }
    writes(body.notice);
    return Response.json({});
  };

  const container = await render({ window: null, error: null });
  const watch = await selectedWorksetWatch();
  await showIssues(watch, issue);
  const row = pendingRow(container, issue.commandId);
  expect(listSessionDispatches).not.toHaveBeenCalled();
  if (from === "conversation") await act(async () => buttonIn(row, "Dismiss")!.click());
  await act(async () => buttonIn(container, "Delivery history")!.click());
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
  expect(dialog.textContent).toContain(issue.reason);
  if (from === "archive") await act(async () => buttonIn(dialog, "Dismiss")!.click());
  const exact = { commandId: issue.commandId, receiptVersion: issue.receiptVersion };
  expect(writes.mock.calls).toEqual([[exact]]);
  expect(lookups.mock.calls).toEqual([[[exact]]]);
  expect(dialog.textContent).toContain("Acknowledged");
  expect(buttonIn(dialog, "Dismiss")).toBeUndefined();
  expect(row.isConnected).toBe(false);

  await act(async () => finishLookup(Response.json({ acknowledgements: [] })));
  expect(buttonIn(dialog, "Dismiss")).toBeUndefined();
  await showIssues(watch, issueReceipt(issue.commandId, "42", "A newer delivery failure"));
  expect(container.textContent).toContain("A newer delivery failure");
  expect(buttonIn(container, "Dismiss")).toBeDefined();
  expect(buttonIn(dialog, "Dismiss")).toBeUndefined();
});

it("keeps older issues actionable after every recent issue is acknowledged", async () => {
  const collection = `/upstream/app/v1/threads/${THREAD.metadata.threadId}/notice-acknowledgements`;
  const recent = Array.from({ length: 20 }, (_, index) =>
    issueReceipt(`recent-${index}`, String(200 + index), "Already acknowledged")
  );
  const prior = issueReceipt("older-receipt", "9007199254740993", "Older failure still needs attention");
  const retainedRow = dispatchFromJson({ ...prior, sessionId: THREAD.metadata.threadId });
  vi.mocked(listSessionDispatches).mockImplementation(async (_, options) =>
    create(ListSessionDispatchesResponseSchema, {
      dispatches: options?.pageToken ? [retainedRow] : [],
      nextPageToken: options?.pageToken ? "" : "oldest",
    })
  );
  const writes: unknown[] = [];
  const acknowledged = new Map(recent.map(({ commandId, receiptVersion }) => [commandId, receiptVersion]));
  deliveryRequest = (url, init) => {
    const body = JSON.parse(String(init?.body));
    if (url.pathname === `${collection}:batchGet`)
      return Response.json({
        acknowledgements: body.notices
          .filter(
            ({ commandId, receiptVersion }: { commandId: string; receiptVersion: string }) =>
              acknowledged.get(commandId) === receiptVersion
          )
          .map((notice: object) => ({ notice })),
      });
    expect(url.pathname).toBe(collection);
    writes.push(body);
    acknowledged.set(body.notice.commandId, body.notice.receiptVersion);
    return Response.json({});
  };
  const container = await render();
  const watch = await selectedWorksetWatch();
  await act(async () => {
    watch.send({
      workset: { sessionId: THREAD.metadata.threadId, recentIssues: recent, moreRecentIssuesAvailable: true },
      resumeToken: "clipped-workset",
    });
  });
  expect(container.textContent).not.toContain("Already acknowledged");
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands).toHaveLength(0);
  await act(async () => buttonIn(container, "Older delivery issues")!.click());
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
  await act(async () => buttonIn(dialog, "Older")!.click());
  expect(dialog.textContent).toContain(prior.reason);
  await act(async () => buttonIn(dialog, "Dismiss")!.click());
  expect(writes).toEqual([{ notice: { commandId: prior.commandId, receiptVersion: prior.receiptVersion } }]);
  expect(buttonIn(dialog, "Dismiss")).toBeUndefined();
  expect(dialog.textContent).toContain(prior.reason);
  expect(buttonIn(container, "Older delivery issues")).toBeDefined();
});

it("keeps a newer receipt visible when its acknowledgement lookup fails", async () => {
  const earlier = issueReceipt("fresh-revision", "40", "Earlier failure");
  const newer = issueReceipt(earlier.commandId, "41", "A new delivery problem");
  deliveryRequest = (_url, init) => {
    const { notices } = JSON.parse(String(init?.body));
    if (notices[0].receiptVersion === newer.receiptVersion)
      return Response.json({ message: "temporarily unavailable" }, { status: 503 });
    return Response.json({ acknowledgements: [{ notice: notices[0] }] });
  };
  const container = await render();
  const watch = await selectedWorksetWatch();
  await showIssues(watch, earlier);
  expect(container.textContent).not.toContain(earlier.reason);
  await showIssues(watch, newer);
  expect(container.textContent).toContain(newer.reason);
  expect(container.textContent).toContain("Could not load acknowledgements.");
});

it("ignores an obsolete acknowledgement batch after the same Dispatcher issue advances", async () => {
  const old = issueReceipt("new-revision", "123");
  let finishOld: ((value: Response) => void) | undefined;
  const batchSizes: number[] = [];
  deliveryRequest = (_url, init) => {
    const { notices } = JSON.parse(String(init?.body));
    batchSizes.push(notices.length);
    if (notices.some((notice: { receiptVersion: string }) => notice.receiptVersion === "123")) {
      return new Promise((resolve) => {
        finishOld = resolve;
      });
    }
    return Response.json({ acknowledgements: [] });
  };
  const container = await render();
  const watch = await selectedWorksetWatch();
  await showIssues(watch, old);
  await showIssues(watch, issueReceipt(old.commandId, "124", "Current issue"));
  await act(async () =>
    finishOld?.(Response.json({ acknowledgements: [{ notice: { commandId: old.commandId, receiptVersion: "123" } }] }))
  );
  expect(container.textContent).toContain("Current issue");
  expect(batchSizes).toEqual([1, 1]);
});

it("revisits paginated acknowledgements on a fresh browser while History is unavailable", async () => {
  const issue = issueReceipt("persisted-issue", "51");
  const acknowledgedIssue = {
    notice: { commandId: issue.commandId, receiptVersion: issue.receiptVersion },
    acknowledgedAt: ISSUE_TIMESTAMP,
  };
  const seenPages: string[] = [];
  vi.mocked(listSessionDispatches).mockResolvedValue(create(ListSessionDispatchesResponseSchema));
  deliveryRequest = (url) => {
    if (url.pathname.endsWith(":batchGet")) return Response.json({ acknowledgements: [acknowledgedIssue] });
    expect(url.searchParams.get("pageSize")).toBe("25");
    const token = url.searchParams.get("pageToken") ?? "";
    seenPages.push(token);
    const page = token ? Number(token) : 1;
    const acknowledgements = Array.from({ length: 25 }, (_, index) =>
      page === 1 && index === 0
        ? acknowledgedIssue
        : {
            notice: { commandId: `page-${page}-notice-${index}`, receiptVersion: String(page * 100 + index + 1) },
            acknowledgedAt: ISSUE_TIMESTAMP,
          }
    );
    return Response.json({ acknowledgements, ...(page < 5 && { nextPageToken: String(page + 1) }) });
  };
  const container = await render({ window: null, error: null });
  const watch = await selectedWorksetWatch();
  await showIssues(watch, issue);
  expect(container.textContent).not.toContain("Delivery refused");
  await act(async () => buttonIn(container, "Delivery history")!.click());
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
  const tab = [...dialog.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(
    (element) => element.textContent === "Acknowledged"
  );
  await act(async () => tab!.click());
  expect(dialog.textContent).toContain("persisted-issue");
  expect(dialog.textContent).toContain("Delivery refused");
  await showIssues(watch);
  selectedReceipts = [{ ...issue, legacyDeletion: true }];
  await act(async () => buttonIn(dialog, "Inspect original receipt")!.click());
  expect(dialog.textContent).toContain("Delivery refused");
  for (let page = 1; page <= 5; page++) {
    expect(dialog.textContent).toContain(`page-${page}-notice-24`);
    if (page < 5) await act(async () => buttonIn(dialog, "Older")!.click());
  }
  expect(seenPages).toEqual(["", "2", "3", "4", "5"]);
});

it("reads a full legacy reason by command ID and retains the view across expired coverage", async () => {
  const container = await render();
  const watch = await selectedWorksetWatch();
  const updatedAt = "2026-11-20T12:00:00Z";
  const reason = "Sandbox was deleted while the receipt was still pending.";
  const row = {
    commandId: "legacy-dispatch",
    operation: "OPERATION_SUBMIT_INPUT",
    state: "DELIVERY_STATE_UNKNOWN",
    updatedAt,
    receiptVersion: "71",
  };
  selectedReceipts = [{ ...row, legacyDeletion: true, reason }];
  await act(async () => {
    watch.send({
      workset: {
        sessionId: THREAD.metadata.threadId,
        recentIssues: [{ ...row, reason: "Sandbox was deleted", reasonTruncated: true }],
      },
      resumeToken: "legacy-workset",
    });
  });
  const panel = pendingRow(container, row.commandId);
  await act(async () => buttonIn(panel, "Show full reason")?.click());
  expect(receiptBatchRequests.at(-1)?.searchParams.getAll("commandIds")).toEqual([row.commandId]);
  expect(panel.textContent).toContain(reason);
  await act(async () => watch.write('{"error":{"code":11,"message":"coverage expired"}}\n'));
  expect(container.textContent).toContain("Showing the last delivery update while reconnecting.");
  expect(container.textContent).toContain("Some earlier delivery receipts may have expired.");
  expect(panel.textContent).toContain(reason);
  await act(async () => window.dispatchEvent(new Event("online")));
  const replacement = await selectedWorksetWatch(watch);
  expect(replacement.url.searchParams.has("resumeToken")).toBe(false);
  await act(async () => replacement.send({ workset: { sessionId: THREAD.metadata.threadId }, resumeToken: "fresh" }));
  expect(container.textContent).not.toContain(reason);
  expect(container.textContent).not.toContain("while reconnecting");
  expect(container.textContent).toContain("Some earlier delivery receipts may have expired.");
});

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

  const row = pendingRow(container, waiting.commandId);
  await vi.waitFor(() =>
    expect(row.querySelector('[role="status"]')?.textContent).toBe(
      "Outcome unknown: Dispatcher did not return a receipt for this command"
    )
  );
  expect(row.querySelector('[role="alert"]')).toBeNull();
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

it("checks an ambiguous submission against the stored command without resending it", async () => {
  new LocalCommands(THREAD.metadata.threadId).remember(message("hung"));
  let expire!: (reason: unknown) => void;
  vi.mocked(submitInput).mockReturnValueOnce(
    new Promise((_resolve, reject) => {
      expire = reject;
    })
  );
  vi.mocked(getDispatch).mockResolvedValue(
    dispatchFromJson({
      commandId: "hung",
      sessionId: THREAD.metadata.threadId,
      ...COMMAND_TARGET,
      command: toJson(CommandSchema, message("hung")),
      state: "DELIVERY_STATE_RUNNER_ADMITTED",
    })
  );
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
  expect(sentIds()).toEqual(["hung"]);
  expect(getDispatch).toHaveBeenCalledWith(THREAD.metadata.threadId, "hung", expect.any(AbortSignal));
  expect(progressLabel(container, "hung")).toContain("Runner accepted · waiting for agent confirmation");
  expect(progressLabel(container, "hung")).not.toContain("signal timed out");
});

it("leaves a command whose delivery reply was lost unconfirmed", async () => {
  new LocalCommands(THREAD.metadata.threadId).remember(message("lost"));
  vi.mocked(submitInput).mockRejectedValueOnce(new Error("Command admission unconfirmed: 503"));
  const container = await render();
  expect(progressLabel(container, "lost")).toBe("Dispatcher receipt unconfirmed: Command admission unconfirmed: 503");
});

it("keeps authored input when reconnect cannot recover its receipt", async () => {
  vi.mocked(getDispatch).mockRejectedValue(new Error("receipt lookup unavailable"));
  const command = message("offline");
  new LocalCommands(THREAD.metadata.threadId).remember(command);
  vi.mocked(submitInput).mockRejectedValueOnce(new Error("lost reply"));
  const container = await render();
  expect(progressLabel(container, "offline")).toContain("lost reply");
  await act(async () => window.dispatchEvent(new Event("online")));
  expect(getDispatch).toHaveBeenCalledWith(THREAD.metadata.threadId, command.commandId, expect.any(AbortSignal));
  expect(sentIds()).toEqual(["offline"]);
  expect(progressLabel(container, "offline")).toContain("receipt lookup unavailable");
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands[0].command).toEqual(command);
});

it.each([
  ["missing payload", undefined],
  ["conflicting payload", { commandId: "uncertain", submitInput: { text: "different authored text" } }],
])("keeps an attempted command after reload with a %s", async (_label, returnedCommand) => {
  const store = new LocalCommands(THREAD.metadata.threadId);
  const command = message("uncertain");
  store.remember(command);
  expect(store.markAttempted(command.commandId)).toBe(true);
  vi.mocked(getDispatch).mockResolvedValue(
    dispatchFromJson({
      commandId: command.commandId,
      sessionId: THREAD.metadata.threadId,
      ...COMMAND_TARGET,
      ...(returnedCommand ? { command: returnedCommand } : {}),
      state: "DELIVERY_STATE_RUNNER_ADMITTED",
    })
  );
  const container = await render();
  expect(sentIds()).toEqual([]);
  expect(getDispatch).toHaveBeenCalledWith(THREAD.metadata.threadId, command.commandId, expect.any(AbortSignal));
  expect(pendingRow(container, command.commandId).querySelector('[data-stage="unconfirmed"]')).not.toBeNull();
  expect(new LocalCommands(THREAD.metadata.threadId).getSnapshot().commands[0]).toMatchObject({
    command,
    dispatch: null,
  });
});

it("does not retry an old Session after leaving while its recovery read is pending", async () => {
  const command = message("navigate-during-recovery");
  const store = new LocalCommands(THREAD.metadata.threadId);
  store.remember(command);
  expect(store.markAttempted(command.commandId)).toBe(true);
  let finishRecovery!: () => void;
  vi.mocked(getDispatch).mockReturnValueOnce(
    new Promise<null>((resolve) => {
      finishRecovery = () => resolve(null);
    })
  );
  const container = await render();
  const call = vi.mocked(getDispatch).mock.calls[0];
  expect(call).toEqual([THREAD.metadata.threadId, command.commandId, expect.any(AbortSignal)]);
  const view = mounted.find((entry) => entry.container === container)!;
  vi.mocked(getThread).mockImplementation(async (threadId) =>
    threadView({ metadata: { ...THREAD.metadata, threadId } })
  );
  await act(async () =>
    view.root.render(
      <TestLiveProviders>
        {page(threadState(), view.topbarTitle!, view.topbarActions!, undefined, "other-session")}
      </TestLiveProviders>
    )
  );
  expect(call[2]?.aborted).toBe(true);
  await act(async () => finishRecovery());
  expect(submitInput).not.toHaveBeenCalled();
  expect(store.getSnapshot().commands[0]?.command).toEqual(command);
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
  expect(pending.querySelector('[role="alert"]')?.textContent).toBe("Failed: model unavailable");
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
  expect(bubble?.parentElement?.querySelector('[role="alert"]')?.textContent).toBe(
    outcome === "failed" ? status : undefined
  );
  expect(bubble?.parentElement?.querySelector(".agentplane-command-progress-hit")?.getAttribute("aria-label")).toBe(
    outcome === "effected" ? status : undefined
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

it("reads an older original on a fresh Session page while the History window is unavailable", async () => {
  const sid = THREAD.metadata.threadId;
  const old = {
    sessionId: sid,
    commandId: "older",
    ...COMMAND_TARGET,
    state: DeliveryState.UNKNOWN,
    operation: Operation.SUBMIT_INPUT,
    reason: "runner disconnected before confirmation",
  };
  const latest = { ...old, commandId: "newer", state: DeliveryState.RUNNER_ADMITTED, reason: "" };
  vi.mocked(listSessionDispatches).mockImplementation(async (_, options) =>
    create(ListSessionDispatchesResponseSchema, {
      dispatches: [options?.pageToken ? old : latest],
      nextPageToken: options?.pageToken ? "" : "second",
    })
  );
  vi.mocked(getDispatch).mockResolvedValue({
    ...dispatchReceipt("older", "DELIVERY_STATE_UNKNOWN", old.reason, "2026-01-01T00:00:00Z"),
    ...old,
    command: message("older"),
  });
  const container = await render(threadState({ windowError: "History unavailable" }));
  await act(async () => button(container, "More").click());
  const item = menuItem([...document.querySelectorAll('[role="menuitem"]')], "Delivery history");
  await act(async () => (item as HTMLElement).click());
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
  expect(dialog.textContent).toContain("newer");
  expect(getDispatch).not.toHaveBeenCalled();
  await act(async () => buttonIn(dialog, "Older")!.click());
  expect(dialog.textContent).toContain("Outcome unknown");
  expect(dialog.textContent).toContain(old.reason);
  await act(async () => buttonIn(dialog, "Show original command")!.click());
  expect(dialog.textContent).toContain("Test input older");
  expect(getDispatch).toHaveBeenCalledWith(sid, "older", expect.any(AbortSignal));
  expect(submitInput).not.toHaveBeenCalled();
});

it("keeps delivery history reachable before the conversation has any History window", async () => {
  vi.mocked(listSessionDispatches).mockResolvedValue(create(ListSessionDispatchesResponseSchema));
  const container = await render({ ...threadState(), window: null, error: "History offline" });
  expect(container.textContent).toContain("History offline");
  await act(async () => buttonIn(container, "Delivery history")!.click());
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("No retained receipts.");
});
