// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import { ItemKind } from "../../protocol/event_pb";
import { historyRows, rowKey } from "./history_rows";
import { HistoryRowView, pruneCommandErrors } from "./projected_session";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import { testItem } from "./thread_entity_fixture";
import { ThreadSyncContext, type PayloadRef, type ThreadEntity, type ThreadSync } from "./thread_sync";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const roots: ReturnType<typeof createRoot>[] = [];

afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
});

const sync: ThreadSync = {
  Thread: ({ children }) => <>{children}</>,
  useThread: () => ({ window: null, error: null }),
  useCommandRows: () => [],
  usePayload: (reference) => ({ body: `Test body of ${reference.owner_id}`, error: null, retry: () => undefined }),
};

/** One element per history row, each holding what the thread view renders for it. */
async function renderHistory(segments: ThreadEntity[], live: boolean): Promise<HTMLElement[]> {
  const container = document.createElement("div");
  const root = createRoot(container);
  roots.push(root);
  await act(async () =>
    root.render(
      <MantineProvider>
        <ThreadSyncContext.Provider value={sync}>
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

/** The text body of `testItem(cursor, ...)`. */
function textRef(cursor: number): PayloadRef {
  return {
    projection_epoch: "test-epoch",
    owner_cursor: String(cursor),
    owner_id: `test-entity-${cursor}`,
    field: "text",
    revision_cursor: String(cursor),
    generation: "1",
    chunk_count: "1",
  };
}

async function toggle(summary: Element): Promise<void> {
  const details = summary.parentElement as HTMLDetailsElement;
  await act(async () => {
    details.open = !details.open;
    details.dispatchEvent(new Event("toggle"));
  });
}

it("drops request errors after their local commands are dismissed", () => {
  const errors = new Map([
    ["dismissed", "connection lost"],
    ["pending", "request timed out"],
  ]);

  expect(pruneCommandErrors(errors, new Set(["pending"]))).toEqual(new Map([["pending", "request timed out"]]));
  expect(pruneCommandErrors(errors, new Set(errors.keys()))).toBe(errors);
});

it("folds a run of tool calls and reasoning behind its summary until it is opened", async () => {
  const [run] = await renderHistory(
    [
      testItem(1, ItemKind.TOOL_CALL, { tool_name: "test-read", completion: "tool", tool_succeeded: true }),
      testItem(2, ItemKind.REASONING, {}, { textRef: textRef(2) }),
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
      testItem(1, ItemKind.REASONING, {}, { textRef: textRef(1) }),
      testItem(2, ItemKind.ASSISTANT_TEXT, {}, { textRef: textRef(2) }),
    ],
    false
  );
  expect(summaries(reasoning)).toEqual(["Reasoning", "Evidence"]);
  expect(answer.textContent).toContain("Test body of test-entity-2");
  for (const row of [reasoning, answer]) expect(row.textContent).not.toMatch(/assistant/i);
});
