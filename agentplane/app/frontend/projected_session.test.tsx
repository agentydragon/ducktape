// @vitest-environment happy-dom
import { create, toJson } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { EventSchema, ItemKind } from "../../protocol/event_pb";
import { historyRows, rowKey } from "./history_rows";
import { EntityCard, HistoryRowView, pruneCommandErrors } from "./projected_session";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import { testItem } from "./thread_entity_fixture";
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

  it("renders a lifecycle event's details as highlighted JSON", async () => {
    const event = create(EventSchema, {
      observation: { case: "harnessStderr", value: { text: "warning: test stderr" } },
    });
    const container = await renderCard(
      entity("lifecycle", { observation: "harness_stderr", event: toJson(EventSchema, event) }, {}),
      {}
    );
    const details = [...container.querySelectorAll("details")].find(
      (element) => element.querySelector("summary")?.textContent === "Lifecycle details"
    );
    expect(details?.querySelector(".agentplane-hljs .hljs-string")?.textContent).toBe('"warning: test stderr"');
  });
});
