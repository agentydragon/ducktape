// @vitest-environment happy-dom
import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { EventSchema, ItemKind, TurnStatus } from "../../protocol/event_pb";
import { EntityCard, pruneCommandErrors } from "./projected_session";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import { ThreadSyncContext, type PayloadRef, type ThreadEntity, type ThreadSync } from "./thread_sync";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

it("drops request errors after their local commands are dismissed", () => {
  const errors = new Map([
    ["dismissed", "connection lost"],
    ["pending", "request timed out"],
  ]);

  expect(pruneCommandErrors(errors, new Set(["pending"]))).toEqual(new Map([["pending", "request timed out"]]));
  expect(pruneCommandErrors(errors, new Set(errors.keys()))).toBe(errors);
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
            <EntityCard threadId="test-thread" entity={card} live={false} />
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
      const alert = container.querySelector('[role="alert"][data-thread-anchor="1"]')!;
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
    expect(container.querySelector('[role="alert"][data-thread-anchor="1"]')?.textContent).toBe(text);
  });
});
