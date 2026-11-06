import type * as ThreadClientModule from "./client";
// @vitest-environment happy-dom
import {
  ThreadPayloadField,
  ThreadCommandOutcome,
  ThreadFeedStatus,
  FeedStatus,
  type ThreadEntity,
} from "../../../history_service/history_service_pb";
import { AutosuspendBlockerSchema } from "../../../proto/environment_lifecycle_pb";
import { DurationSchema, TimestampSchema, timestampFromDate } from "@bufbuild/protobuf/wkt";
import { readySandbox, suspendedSandbox } from "../testing/sandbox";
import {
  SandboxPhase,
  ReadinessSchema,
  ReadinessState,
  EnvironmentDescriptorSchema,
  type EnvironmentActions,
  type EnvironmentConfiguration,
  type EnvironmentObservedState,
  type KubernetesDiagnostics,
  type EnvironmentDescriptor,
} from "../../../environments/v1/environment_service_pb";

import { clone, create, toJson } from "@bufbuild/protobuf";
import type { LangfuseSearchSettings, TraceSearchSettings } from "../schema";
import { TEST_REASONING_EFFORTS } from "../testing/model_catalog";
import { MantineProvider } from "@mantine/core";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../../proto/command_pb";
import { EventEntrySchema, type EventEntry } from "../../../proto/event_log_pb";
import { ItemKind, RecoveryDisposition, TurnStatus } from "../../../proto/event_pb";
import { HarnessState, LaunchOutcome, SetupState } from "../../../proto/session_pb";
import type * as ModelCatalogModule from "../model_catalog";
import type * as SandboxClientModule from "../sandboxes/client";
import { LANGFUSE_SEARCH_SETTINGS } from "../testing/langfuse_search";
import { api } from "../client";
import {
  type CommandSubmission,
  type CommandTarget,
  dispatchFromJson,
  submitInput,
  changeModel,
  changeReasoningEffort,
  interruptTurn,
  getThread,
  listThreadWindow,
} from "./client";
import { models } from "../model_catalog";
import { resumeSandbox } from "../sandboxes/client";
import { historyRows, rowKey } from "./history_rows";
import { SandboxesLiveProvider, useRequiredSandboxesLive } from "../sandboxes/live";
import { ThreadsLiveProvider, useRequiredThreadsLive } from "./live";
import { LocalCommands } from "./local_commands";
import { STREAMING_CURSOR } from "../../../../util/frontend/markdown";
import { ThreadPage } from "./page";
import { HistoryRowView } from "./history_row_views";
import { RetainedDisclosureProvider } from "../../../../util/frontend/transcript/retained_disclosures";
import { DEGRADED_AFTER_MS, STALE_AFTER_MS } from "../stream_status";
import { THREAD_STATUS_MARKS } from "../status_mark";
import { testItem } from "./testing/entity";
import {
  badgeLabels,
  entity,
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
import { TopbarContext } from "../topbar";
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
  listThreadWindow: vi.fn(),
}));
vi.mock("../model_catalog", async (importOriginal) => ({
  ...(await importOriginal<typeof ModelCatalogModule>()),
  models: vi.fn(),
}));
vi.mock("../sandboxes/client", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxClientModule>()),
  resumeSandbox: vi.fn(),
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
type Inventory = EnvironmentDescriptor[] | null;
type DescriptorChanges = Omit<
  Partial<EnvironmentDescriptor>,
  "configuration" | "observedState" | "actions" | "kubernetesDiagnostics"
> & {
  configuration?: Partial<EnvironmentConfiguration>;
  observedState?: Partial<EnvironmentObservedState>;
  actions?: Partial<EnvironmentActions>;
  kubernetesDiagnostics?: Partial<KubernetesDiagnostics>;
};

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

function updatedSandbox(base: EnvironmentDescriptor, changes: DescriptorChanges): EnvironmentDescriptor {
  const updated = clone(EnvironmentDescriptorSchema, base);
  const { configuration, observedState, actions, kubernetesDiagnostics, ...topLevel } = changes;
  Object.assign(updated, topLevel);
  if (configuration) Object.assign(updated.configuration!, configuration);
  if (observedState) Object.assign(updated.observedState!, observedState);
  if (actions) Object.assign(updated.actions!, actions);
  if (kubernetesDiagnostics) Object.assign(updated.kubernetesDiagnostics!, kubernetesDiagnostics);
  return updated;
}

function autosuspendSandbox(idle_suspend_at: string | null): EnvironmentDescriptor {
  return updatedSandbox(inventorySandbox(), {
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
const TRACE_SEARCH_SETTINGS: TraceSearchSettings = {
  grafana_base_url: "https://grafana.test.invalid",
  tempo_datasource_uid: "tempo-test",
  deployment_environment: "agentplane-test",
};
let traceSearchSettings: TraceSearchSettings | null = TRACE_SEARCH_SETTINGS;
let langfuseSearchSettings: LangfuseSearchSettings | null = LANGFUSE_SEARCH_SETTINGS;
// The Dispatcher watches the page opened, which a test answers itself.
let dispatchWatches: ServedWatch[] = [];
let threadRows: ThreadView[] | null = null;
let noticeInboxes: Array<{ inboxId: string; sessionId: string; queued: number; unacknowledged: number }> = [];

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
  vi.spyOn(api, "GET").mockImplementation((async (path: string) => {
    const data =
      path === "/frontend-config" ? { trace_search: traceSearchSettings, langfuse_search: langfuseSearchSettings } : {};
    return { data, response: Response.json(data) };
  }) as never);
  vi.mocked(getThread).mockResolvedValue(THREAD);
  vi.mocked(models).mockResolvedValue({
    models: [{ model: "test-model", display_name: "Test Model", reasoning_efforts: TEST_REASONING_EFFORTS }],
    harnesses: { HARNESS_CLAUDE: ["test-model"], HARNESS_CODEX: [] },
  });
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
        url.pathname === "/upstream/app/v1/threads:watch" ||
        url.pathname === "/upstream/history/v1/session-summaries:watch"
      ) {
        const stream = new ServedWatch(String(input), init?.signal ?? undefined);
        if (url.pathname === "/upstream/app/v1/threads:watch") {
          stream.send({ changed: {}, updatesConnected: true });
        } else {
          stream.send({ changed: {} });
        }
        return Promise.resolve(stream.response);
      }
    }
    return sandboxFetch(input, init);
  });
  vi.mocked(listThreadWindow).mockImplementation(async () => ({
    threads: threadRows ?? [
      (() => {
        const sharedSummary = sharedThread.summary;
        const attachedOverride =
          sharedSummary && Object.hasOwn(sharedSummary, "attached") ? sharedSummary.attached : THREAD.summary.attached;
        const attached =
          attachedOverride === undefined ? undefined : { ...THREAD.summary.attached, ...attachedOverride };
        return threadView({
          metadata: { ...THREAD.metadata, ...sharedThread.metadata },
          summary: {
            ...THREAD.summary,
            ...sharedSummary,
            feedStatus: sharedSummary?.feedStatus ?? sharedFeed,
            attached,
          },
        });
      })(),
    ],
    more: false,
    archived_count: 0,
  }));
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
  const container = await render();
  const menuButton = container.querySelector<HTMLButtonElement>('button[aria-label="More"]');
  if (!menuButton) throw new Error("Missing thread actions menu");
  await act(async () => menuButton.click());
  const link = [...document.querySelectorAll('[role="menuitem"]')].find((node) =>
    node.textContent?.includes("Find Thread traces in Grafana")
  );
  expect(link).toBeInstanceOf(HTMLAnchorElement);
  expect(new URL((link as HTMLAnchorElement).href).searchParams.get("panes")).toContain("span.session.id");
});

it("offers a Sandbox-scoped Langfuse link from the active Thread menu", async () => {
  const container = await render();
  const menuButton = container.querySelector<HTMLButtonElement>('button[aria-label="More"]');
  if (!menuButton) throw new Error("Missing thread actions menu");
  await act(async () => menuButton.click());
  const link = [...document.querySelectorAll('[role="menuitem"]')].find((node) =>
    node.textContent?.includes("Sandbox LLM traces in Langfuse")
  );
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
  const container = await render();
  const menuButton = container.querySelector<HTMLButtonElement>('button[aria-label="More"]');
  if (!menuButton) throw new Error("Missing thread actions menu");
  await act(async () => menuButton.click());
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].some((node) =>
      node.textContent?.includes("Sandbox LLM traces in Langfuse")
    )
  ).toBe(true);
});

it("hides the Grafana trace link from the thread menu when unconfigured", async () => {
  traceSearchSettings = null;
  const container = await render();
  const menuButton = container.querySelector<HTMLButtonElement>('button[aria-label="More"]');
  if (!menuButton) throw new Error("Missing thread actions menu");
  await act(async () => menuButton.click());
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].some((node) =>
      node.textContent?.includes("Find Thread traces in Grafana")
    )
  ).toBe(false);
});

it("hides only the Langfuse link when Langfuse search is unconfigured", async () => {
  langfuseSearchSettings = null;
  const container = await render();
  const menuButton = container.querySelector<HTMLButtonElement>('button[aria-label="More"]');
  if (!menuButton) throw new Error("Missing thread actions menu");
  await act(async () => menuButton.click());
  const menuItems = [...document.querySelectorAll('[role="menuitem"]')];
  expect(menuItems.some((node) => node.textContent?.includes("Sandbox LLM traces in Langfuse"))).toBe(false);
  expect(menuItems.some((node) => node.textContent?.includes("Find Thread traces in Grafana"))).toBe(true);
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

function sentOperations(): unknown[] {
  const sent = [
    ...vi.mocked(submitInput).mock.calls.map(([, , , text], index) => ({
      order: vi.mocked(submitInput).mock.invocationCallOrder[index],
      operation: { case: "submitInput", value: { text } },
    })),
    ...vi.mocked(changeModel).mock.calls.map(([, , , model], index) => ({
      order: vi.mocked(changeModel).mock.invocationCallOrder[index],
      operation: { case: "changeModel", value: { model } },
    })),
    ...vi.mocked(changeReasoningEffort).mock.calls.map(([, , , effort], index) => ({
      order: vi.mocked(changeReasoningEffort).mock.invocationCallOrder[index],
      operation: { case: "changeReasoningEffort", value: { effort } },
    })),
    ...vi.mocked(interruptTurn).mock.calls.map(([, , , turnId], index) => ({
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
      attached: { activeTurnId: "running" },
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
    updatedSandbox(autosuspendSandbox(null), {
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
    updatedSandbox(autosuspendSandbox(null), {
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
    });
    threadRows = [THREAD, sibling];
    sharedFeed = FeedStatus.ENDED;
    sandboxes = [
      updatedSandbox(autosuspendSandbox(deadline), {
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
    updatedSandbox(autosuspendSandbox("2026-01-01T12:12:00Z"), {
      observedState: {
        autosuspendBlockers: [
          create(AutosuspendBlockerSchema, { reason: "ActiveTurn", sessionId: THREAD.metadata.threadId }),
        ],
      },
    }),
  ];
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
    updatedSandbox(autosuspendSandbox(null), {
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
    updatedSandbox(autosuspendSandbox(null), {
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

it.each([["a past deadline", "2026-01-01T11:59:00Z", "Autosuspend pending"]])(
  "handles %s without showing a negative countdown",
  async (_case, deadline, expected) => {
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-01-01T12:00:00Z"));
    sandboxes = [autosuspendSandbox(deadline)];
    const container = await render();
    expect(container.querySelector(".agentplane-composer-autosuspend")?.textContent).toBe(expected);
  }
);

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
  sandboxes = [updatedSandbox(inventorySandbox("suspended", true), { observedState: { suspendedBy: "Requested" } })];
  sharedThread = {
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
  expect(vi.mocked(submitInput).mock.calls).toHaveLength(1);
  expect(vi.mocked(submitInput).mock.calls[0]?.[0]).toBe(THREAD.metadata.threadId);
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "Continue after resume" } }]);
});

it("disables the composer and offers resume for a fresh suspended Sandbox without wake-on-message", async () => {
  vi.mocked(resumeSandbox).mockResolvedValue(undefined);
  sandboxes = [updatedSandbox(inventorySandbox("suspended", false), { observedState: { suspendedBy: "Requested" } })];
  sharedThread = {
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
  "does not queue messages for a suspended Session whose setup is %s",
  async (setupState) => {
    sandboxes = [inventorySandbox("suspended", true)];
    sharedThread = {
      summary: {
        feedStatus: FeedStatus.ENDED,
        attached: {
          harnessState: HarnessState.STOPPED,
          setupState,
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
        attached: { harnessState: HarnessState.STOPPED, launchOutcome: LaunchOutcome.PENDING },
      },
    },
  ],
  [
    "a Session whose harness exited",
    {
      summary: {
        feedStatus: FeedStatus.ENDED,
        attached: { harnessState: HarnessState.STOPPED, launchOutcome: LaunchOutcome.STARTED },
      },
    },
  ],
] as const)("sends input to %s without an explicit resume action", async (_name, snapshot) => {
  sharedThread = snapshot;
  const dormant = threadState({ rows: [viewState({ harness: "stopped", status: ThreadFeedStatus.ENDED })] });
  const container = await render(dormant);
  expect(composer(container).disabled).toBe(false);
  expect(document.body.textContent).not.toContain("Resume harness");
  expect(container.querySelector(".agentplane-thread-status-indicator")?.getAttribute("aria-label")).toBe(
    "Harness not running"
  );
  await type(composer(container), "Wake for this message");
  await press(composer(container), {});
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "Wake for this message" } }]);
  expect(vi.mocked(submitInput).mock.calls[0]?.[0]).toBe(THREAD.metadata.threadId);
});

it("queues input while one-shot setup runs", async () => {
  sharedThread = { summary: { attached: { setupState: SetupState.RUNNING, harnessState: HarnessState.STOPPED } } };
  const container = await render(
    threadState({ rows: [viewState({ harness: "stopped", status: ThreadFeedStatus.ENDED })] })
  );
  expect(composer(container).disabled).toBe(false);
  await type(composer(container), "Continue after setup");
  await press(composer(container), {});
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "Continue after setup" } }]);
});

it.each([
  ["failed Thread operation", threadState({ rows: [viewState({ status: ThreadFeedStatus.FAILED })] })],
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

it.each([
  [{ summary: { feedStatus: FeedStatus.FAILED } }, "Runner feed failed", "failed"],
  [{ summary: { feedStatus: FeedStatus.ENDED } }, "Runner feed ended", "inactive"],
  [
    {
      summary: {
        feedStatus: FeedStatus.ACTIVE,
        attached: { harnessState: HarnessState.STOPPED, launchOutcome: LaunchOutcome.PENDING },
      },
    },
    "Harness not running",
    "stopped",
  ],
  // A stopped native harness can outlive its last attached feed.
  [
    { summary: { feedStatus: FeedStatus.ENDED, attached: { harnessState: HarnessState.STOPPED } } },
    "Harness not running",
    "stopped",
  ],
  [{ summary: { attached: { harnessState: HarnessState.STOPPED } } }, "Harness not running", "stopped"],
  [
    { summary: { attached: { activeTurnId: "turn-1" } } },
    "Turn running · Runner feed active · harness running",
    "running",
  ],
  [{}, "Runner feed active · harness running", "idle"],
  [
    { summary: { lastTurnStatus: TurnStatus.FAILED } },
    "Turn failed · Runner feed active · harness running",
    "turn_error",
  ],
  [
    { summary: { lastTurnStatus: TurnStatus.PROCESS_LOST } },
    "Turn lost · Runner feed active · harness running",
    "turn_error",
  ],
] as const)("uses shared thread row %o for the topbar status", async (row, label, kind) => {
  sharedThread = row;
  const container = await render();
  const indicator = container.querySelector(".agentplane-thread-status-indicator");
  expect(mounted.at(-1)?.topbarTitle?.contains(indicator ?? null)).toBe(true);
  expect(indicator?.getAttribute("aria-label")).toBe(label);
  expect(indicator?.getAttribute("data-status")).toBe(kind);
});

it("reads the Sandbox for the topbar and the composer from the one inventory stream", async () => {
  sandboxes = [updatedSandbox(inventorySandbox("suspended", true), { observedState: { suspendedBy: "Requested" } })];
  const container = await render();
  const indicator = mounted.at(-1)?.topbarTitle?.querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("data-status")).toBe("suspended");
  expect(composer(container).placeholder).toMatch(/^Sandbox is suspended \(on request\)\./);
});

it("shows the thread's unprocessed notification count in its topbar status indicator", async () => {
  noticeInboxes = [{ inboxId: "inbox-1", sessionId: THREAD.metadata.threadId, queued: 2, unacknowledged: 1 }];
  await render();
  const indicator = mounted.at(-1)?.topbarTitle?.querySelector(".agentplane-thread-status-indicator");
  expect(mounted.at(-1)?.topbarTitle?.contains(indicator ?? null)).toBe(true);
  expect(indicator?.querySelector(".agentplane-thread-list-row-notices")?.textContent).toBe("3");
});

const faviconSvg = (): string => decodeURIComponent(favicon.getAttribute("href")?.split(",")[1] ?? "");

it("shows green chevrons in the status indicator and the favicon, and a leading glyph in the title, while a turn is active", async () => {
  vi.useFakeTimers();
  sharedThread = { summary: { attached: { activeTurnId: "turn-1" } } };
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
  sharedThread = { summary: { lastTurnStatus: TurnStatus.FAILED } };
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("data-status")).toBe("turn_error");
  expect(indicator?.querySelector("svg")).not.toBeNull();
  expect(faviconSvg()).toContain(`fill="${THREAD_STATUS_MARKS.turn_error.color}"`);
  // Not the red dot of a failed runner feed, which has a white inner ring.
  expect(faviconSvg()).not.toContain('stroke="#fff"');
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.turn_error.glyph} Test thread · Turn failed — Agentplane`);
});

it("shows a stopped harness as an icon in the status indicator and the favicon, with its own title glyph", async () => {
  sharedThread = { summary: { attached: { harnessState: HarnessState.STOPPED } } };
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("data-status")).toBe("stopped");
  expect(indicator?.querySelector("svg")).not.toBeNull();
  expect(faviconSvg()).toContain(`stroke="${THREAD_STATUS_MARKS.stopped.color}"`);
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.stopped.glyph} Test thread · Stopped — Agentplane`);
});

it("shows a suspended Sandbox as paused in the status indicator, favicon and thread title", async () => {
  sandboxes = [inventorySandbox("suspended")];
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("aria-label")).toBe("Sandbox suspended");
  expect(indicator?.getAttribute("data-status")).toBe("suspended");
  expect(indicator?.querySelector("svg")).not.toBeNull();
  expect(faviconSvg()).toContain(`fill="${THREAD_STATUS_MARKS.suspended.color}"`);
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.suspended.glyph} Test thread · Suspended — Agentplane`);
});

it.each<["Idle" | "Requested", string]>([
  ["Idle", "idle"],
  ["Requested", "on request"],
])("says a Sandbox suspended by %s was suspended %s, in the indicator and composer", async (suspendedBy, reason) => {
  sandboxes = [updatedSandbox(inventorySandbox("suspended"), { observedState: { suspendedBy } })];
  const container = await render();
  expect(container.querySelector(".agentplane-thread-status-indicator")?.getAttribute("aria-label")).toBe(
    `Sandbox suspended (${reason})`
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
const waking: EnvironmentDescriptor = updatedSandbox(autosuspendSandbox(null), {
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
      updatedSandbox(starting, {
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
    updatedSandbox(running, {
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

it("does not show active-turn status when the runner is not active", async () => {
  sharedFeed = FeedStatus.ENDED;
  sharedThread = { summary: { attached: { activeTurnId: "turn-1" } } };
  const indicator = (await render()).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("aria-label")).toBe("Runner feed ended");
  expect(indicator?.getAttribute("data-status")).toBe("inactive");
});

// Retained history still says the harness runs and the feed failed; an archived thread shows the archive
// icon instead, in the status indicator, the favicon and the title glyph.
it("shows an archived thread as the archive icon, in the favicon and with its own title glyph", async () => {
  vi.mocked(getThread).mockResolvedValue(
    threadView({ metadata: { ...THREAD.metadata, archived: true }, summary: THREAD.summary })
  );
  sharedThread = { metadata: { archived: true } };
  sandboxes = [inventorySandbox()];
  const indicator = (
    await render(threadState({ rows: [viewState({ status: ThreadFeedStatus.FAILED })] }))
  ).querySelector(".agentplane-thread-status-indicator");
  expect(indicator?.getAttribute("aria-label")).toBe("Thread archived");
  expect(indicator?.getAttribute("data-status")).toBe("archived");
  expect(indicator?.querySelector("svg")).not.toBeNull();
  expect(faviconSvg()).toContain(`stroke="${THREAD_STATUS_MARKS.archived.color}"`);
  expect(faviconSvg()).not.toContain("<circle");
  expect(document.title).toBe(`${THREAD_STATUS_MARKS.archived.glyph} Test thread · Archived — Agentplane`);
});

it.each([[{ metadata: { archived: false } }, [], "Sandbox unavailable", "inactive"]])(
  "shows unavailable inventory state for thread %o with sandboxes %o: %s",
  async (overrides, inventory, label, kind) => {
    vi.mocked(getThread).mockResolvedValue(
      threadView({ metadata: { ...THREAD.metadata, ...overrides.metadata }, summary: THREAD.summary })
    );
    sharedThread = overrides;
    sandboxes = inventory;
    const indicator = (
      await render(threadState({ rows: [viewState({ status: ThreadFeedStatus.FAILED })] }))
    ).querySelector(".agentplane-thread-status-indicator");
    expect(indicator?.getAttribute("aria-label")).toBe(label);
    expect(indicator?.getAttribute("data-status")).toBe(kind);
  }
);

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

// Each of these but the first disables the controls, which the composer's indicator reports only as
// "Sandbox unavailable"; the header says why: the state the inventory last reported, or that its
// absence from one that is stale or down past the grace proves nothing. A drop within the grace is a
// blip, on which the last inventory still stands. A stale or dropped inventory is reported by the
// connection indicator, never by an alert of its own.
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
    if (state === "setup") sharedThread = { summary: { attached: { setupState: SetupState.RUNNING } } };
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

async function admit(
  _threadId: string,
  _target: CommandTarget | null,
  commandId: string,
  text: string
): Promise<CommandSubmission> {
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
  return vi.mocked(submitInput).mock.calls.map(([, , commandId]) => commandId);
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
  const store = new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET);
  store.remember(message("retained-admitted"));
  store.acknowledge(message("retained-admitted"), admission(message("retained-admitted")));
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
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(queued);
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
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(failed);
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
    for (const dispatch of rows) watch.send({ dispatch });
    watch.send({ caughtUp: {} });
  });
}

it("replaces a queued Dispatcher receipt with the failure its stream reports", async () => {
  const waiting = message("queued-then-failed");
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(waiting);
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
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(waiting);
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
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(waiting);
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
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(message("touch"));
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
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(message("unsent"));
  const container = await render();
  expect(vi.mocked(submitInput)).not.toHaveBeenCalled();
  const row = pendingRow(container, "unsent");
  expect(buttonIn(row, "Cancel")).toBeDefined();
  expect(buttonIn(row, "Retry")).toBeUndefined();
  await act(async () => buttonIn(row, "Cancel")?.click());
  expect(new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).getSnapshot().commands).toEqual([]);
  online.mockReturnValue(true);
  await act(async () => window.dispatchEvent(new Event("online")));
  expect(vi.mocked(submitInput)).not.toHaveBeenCalled();
});

it("does not offer Cancel once an offline command has begun its first HTTP attempt", async () => {
  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(message("queued"));
  const container = await render();
  expect(buttonIn(pendingRow(container, "queued"), "Cancel")).toBeDefined();
  online.mockReturnValue(true);
  await act(async () => window.dispatchEvent(new Event("online")));
  expect(sentIds()).toEqual(["queued"]);
  expect(new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).cancelUnsent("queued")).toBe(false);
  expect(buttonIn(pendingRow(container, "queued"), "Cancel")).toBeUndefined();
});

it("shows a delivery that outlives its deadline as unconfirmed and retriable", async () => {
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(message("hung"));
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
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(message("lost"));
  vi.mocked(submitInput).mockRejectedValueOnce(new Error("Command admission unconfirmed: 503"));
  const container = await render();
  expect(progressLabel(container, "lost")).toBe("Dispatcher receipt unconfirmed: Command admission unconfirmed: 503");
});

it("redelivers an unconfirmed command when the browser comes back online", async () => {
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(message("offline"));
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
  new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).remember(
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
      operation: "change_model",
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
  expect(new LocalCommands(THREAD.metadata.threadId, COMMAND_TARGET).getSnapshot().commands).toHaveLength(0);
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
            operation: "submit_input",
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
            operation: "submit_input",
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
        value: { operation: "change_model", ...value },
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
      operation: "submit_input",
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

it("shows a lone reasoning step as its own reasoning block, and assistant text without a role label", async () => {
  const [reasoning, answer] = await renderHistory(
    [
      testItem(1, ItemKind.REASONING, {}, { textRef: reference("test-entity-1", ThreadPayloadField.TEXT) }),
      testItem(2, ItemKind.ASSISTANT_TEXT, {}, { textRef: reference("test-entity-2", ThreadPayloadField.TEXT) }),
    ],
    false,
    {
      "test-entity-1:text": "Reasoning preview body",
      "test-entity-2:text": "Test body of test-entity-2",
    }
  );
  expect(reasoning.querySelector(".transcript-step-details")).toBeNull();
  expect(reasoning.querySelector(".transcript-step-title")?.textContent).toBe("Reasoning");
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

  it("summarizes recovery in a collapsed tool run without marking recovered items streaming", async () => {
    const [run] = await renderHistory(
      [
        testItem(1, ItemKind.TOOL_CALL, { completion: { case: undefined }, recovery: RecoveryDisposition.UNKNOWN }),
        testItem(2, ItemKind.TOOL_CALL, {
          completion: { case: "tool", value: { succeeded: false } },
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
          toolName: "test-read",
          completion: { case: "tool", value: { succeeded: true } },
          recovery: RecoveryDisposition.RETAINED,
        }),
        testItem(
          2,
          ItemKind.REASONING,
          { recovery: RecoveryDisposition.RETAINED },
          { textRef: reference("test-entity-2", ThreadPayloadField.TEXT) }
        ),
        testItem(3, ItemKind.TOOL_CALL, {
          toolName: "test-shell",
          completion: { case: "tool", value: { succeeded: true } },
          recovery: RecoveryDisposition.RETAINED,
        }),
      ],
      false,
      { "test-entity-2:text": "Kept in context" }
    );
    const summary = run.querySelector(".ui-disclosure-summary")!;
    expect(summary.textContent).toContain("2 tool calls, 1 reasoning step");
    expect(badgeLabels(run)).toEqual([]);

    await toggle(summary);
    expect(run.textContent).toContain("test-read");
    expect(badgeLabels(run)).toEqual([]);
  });
});
