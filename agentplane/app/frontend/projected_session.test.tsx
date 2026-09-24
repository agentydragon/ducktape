// @vitest-environment happy-dom

import { create, equals, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import { EventSchema, ItemKind, TurnStatus } from "../../protocol/event_pb";
import { command, getThread, models, type ThreadView } from "./client";
import { historyRows, rowKey } from "./history_rows";
import { LocalCommands } from "./local_commands";
import { EntityCard, HistoryRowView, ProjectedSession, pruneCommandErrors } from "./projected_session";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import { testItem } from "./thread_entity_fixture";
import {
  ThreadSyncContext,
  type PayloadRef,
  type ThreadEntity,
  type ThreadState,
  type ThreadSync,
} from "./thread_sync";

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./client")>()),
  command: vi.fn(),
  getThread: vi.fn(),
  models: vi.fn(),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
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
};

// What the sandbox inventory stream reports. By default the thread's sandbox is running, so its
// controls are live.
let sandboxes: Array<{ name: string; state: string }> = [];

beforeEach(() => {
  localStorage.clear();
  sandboxes = [{ name: THREAD.sandbox, state: "running" }];
  vi.mocked(getThread).mockResolvedValue(THREAD);
  vi.mocked(models).mockResolvedValue({ HARNESS_CLAUDE: ["test-model"], HARNESS_CODEX: [] });
  vi.mocked(command).mockReturnValue(new Promise(() => {}));
  vi.stubGlobal(
    "EventSource",
    class extends EventTarget {
      constructor() {
        super();
        queueMicrotask(() =>
          this.dispatchEvent(
            new MessageEvent("snapshot", {
              data: JSON.stringify({
                sandboxes,
                watch: { fresh: true, stale_after_seconds: 90, refreshed_seconds_ago: { sandboxes: 0 } },
              }),
            })
          )
        );
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
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

function viewState({
  harness = "running",
  status = "active",
  model = "test-model",
}: {
  harness?: string | null;
  status?: "active" | "ended" | "failed";
  model?: string | null;
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
      controls: { applied_model: model, active_turn_id: null, harness_state: harness },
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
  windowError = null,
  error = null,
}: {
  rows?: ThreadEntity[];
  caughtUp?: boolean;
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
      error: windowError,
      refresh: () => {},
    },
    error,
  };
}

async function render(state: ThreadState = threadState()): Promise<HTMLDivElement> {
  const sync: ThreadSync = {
    Thread: ({ children }) => <>{children}</>,
    useThread: () => state,
    useCommandRows: () => [],
    usePayload: () => ({ body: null, error: null, retry: () => {} }),
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => {
    root.render(
      <MantineProvider env="test">
        <ThreadSyncContext.Provider value={sync}>
          <ProjectedSession threadId={THREAD.id} onBack={() => {}} />
        </ThreadSyncContext.Provider>
      </MantineProvider>
    );
  });
  return container;
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

it.each<KeyboardEventInit>([{ ctrlKey: true }, { metaKey: true }])(
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
  const field = composer(await render());
  await type(field, "hello");
  await press(field, {});
  expect(sentOperations()).toMatchObject([{ case: "submitInput", value: { text: "hello" } }]);
  expect(field.value).toBe("");
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

// Each row's lower-severity axes disagree with the one that decides the dot. Only a sync that is
// still settling breathes.
it.each([
  [
    { windowError: "test shape gone", error: "test fetch failed" },
    "red",
    false,
    "Thread sync stopped: test shape gone",
  ],
  [{ error: "test fetch failed", rows: [viewState({ harness: "lost" })] }, "yellow", true, "Reconnecting…"],
  [{ caughtUp: false, rows: [viewState({ status: "failed" })] }, "yellow", true, "Catching up…"],
  [{ rows: [viewState({ status: "failed", harness: "lost" })] }, "red", false, "Runner feed failed"],
  [{ rows: [viewState({ status: "ended", harness: "lost" })] }, "red", false, "Harness lost"],
  [{ rows: [viewState({ status: "ended" })] }, "gray", false, "Runner feed ended · harness running"],
  [{ rows: [viewState({ harness: null })] }, "yellow", false, "No harness observed"],
  [{ rows: [viewState({ harness: "stopped" })] }, "gray", false, "Runner feed active · harness stopped"],
  [{ rows: [viewState()] }, "green", false, "Runner feed active · harness running"],
])("collapses %o into a %s dot (breathing: %s): %s", async (state, color, breathing, label) => {
  const dot = (await render(threadState(state))).querySelector(".agentplane-status-dot");
  expect(dot?.getAttribute("aria-label")).toBe(label);
  expect(dot?.getAttribute("style")).toContain(`--mantine-color-${color}-6`);
  expect(dot?.classList.contains("agentplane-breathing-dot")).toBe(breathing);
});

// Retained history still says the harness runs and the feed failed; neither is live any more.
it.each([
  [{ archived: true }, [{ name: THREAD.sandbox, state: "running" }], "Thread archived"],
  [{ archived: false }, [], "Sandbox unavailable"],
  [{ archived: false }, [{ name: THREAD.sandbox, state: "suspended" }], "Sandbox unavailable"],
])("shows a gray dot for thread %o with sandboxes %o: %s", async (overrides, inventory, label) => {
  vi.mocked(getThread).mockResolvedValue({ ...THREAD, ...overrides });
  sandboxes = inventory;
  const dot = (await render(threadState({ rows: [viewState({ status: "failed" })] }))).querySelector(
    ".agentplane-status-dot"
  );
  expect(dot?.getAttribute("aria-label")).toBe(label);
  expect(dot?.getAttribute("style")).toContain("--mantine-color-gray-6");
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

async function renderCard(card: ThreadEntity, bodies: Record<string, string>): Promise<HTMLDivElement> {
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
              <EntityCard threadId="test-thread" entity={card} live={false} />
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
    false
  );
  const summary = run.querySelector("summary")!;
  expect(summary.textContent).toContain("2 tool calls, 1 reasoning step");
  expect(summary.querySelector('[role="img"][aria-label="Failed"]')).not.toBeNull();
  expect(run.textContent).not.toContain("test-read");

  await toggle(summary);
  expect(run.textContent).toContain("test-read");
  expect(run.textContent).toContain("test-shell");
  expect(summaries(run)).toContain("Reasoning");
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

it("shows a lone reasoning step as its own reasoning block, and assistant text without a role label", async () => {
  const [reasoning, answer] = await renderHistory(
    [
      testItem(1, ItemKind.REASONING, {}, { textRef: reference("test-entity-1", "text") }),
      testItem(2, ItemKind.ASSISTANT_TEXT, {}, { textRef: reference("test-entity-2", "text") }),
    ],
    false,
    { "test-entity-2:text": "Test body of test-entity-2" }
  );
  expect(summaries(reasoning)).toEqual(["Reasoning"]);
  expect(answer.textContent).toContain("Test body of test-entity-2");
  for (const row of [reasoning, answer]) expect(row.textContent).not.toMatch(/assistant/i);
});

const PROSE = "Run **every** test\n- first";

describe("EntityCard", () => {
  it("renders a tool call's JSON arguments highlighted and its plain output verbatim, both as code", async () => {
    const container = await renderCard(
      entity(
        "item",
        { kind: ItemKind.TOOL_CALL, tool_name: "Bash", completion: "", tool_succeeded: true },
        { argumentsRef: reference("test-tool", "arguments"), outputRef: reference("test-tool", "output") }
      ),
      { "test-tool:arguments": '{"command": "ls", "timeout": 30}', "test-tool:output": PROSE }
    );

    const args = (await disclose(container, "Arguments")).querySelector(".agentplane-hljs");
    expect(args?.querySelector(".hljs-attr")?.textContent).toBe('"command"');
    expect(args?.querySelector(".hljs-string")?.textContent).toBe('"ls"');
    expect(args?.querySelector(".hljs-number")?.textContent).toBe("30");

    const output = (await disclose(container, "Output")).querySelector(".agentplane-hljs");
    expect(output?.textContent).toBe(PROSE);
    expect(output?.querySelector("[class^='hljs-'], strong, li")).toBeNull();
  });

  it("renders the assistant's text as Markdown", async () => {
    const container = await renderCard(
      entity(
        "item",
        { kind: ItemKind.ASSISTANT_TEXT, tool_name: "", completion: PROSE, tool_succeeded: null },
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
