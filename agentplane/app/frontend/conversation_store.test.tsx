// @vitest-environment happy-dom

import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

const captured = vi.hoisted(
  (): { options: Array<{ id: string; shapeOptions: { onError?: (reason: unknown) => void } }> } => ({
    options: [],
  })
);

vi.mock("@tanstack/electric-db-collection", () => ({
  electricCollectionOptions: (options: { id: string; shapeOptions: { onError?: (reason: unknown) => void } }) => {
    captured.options.push(options);
    return options;
  },
}));

vi.mock("@tanstack/react-db", () => ({
  createCollection: <T,>(options: T) => ({ ...options, isReady: () => true }),
  useLiveQuery: () => ({ data: [], isError: false }),
}));

import { CommandSelection, PayloadBody, PendingCommandPages, type PayloadRef } from "./conversation_store";

let root: ReturnType<typeof createRoot> | undefined;

async function render(element: ReactNode): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(element));
  return container;
}

async function rerender(element: ReactNode): Promise<void> {
  await act(async () => root?.render(element));
}

afterEach(async () => {
  await act(async () => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  captured.options.splice(0);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function option(prefix: string) {
  const found = captured.options.find((value) => value.id.startsWith(prefix));
  expect(found).toBeDefined();
  return found!;
}

it("wires command terminal callbacks to the current collection and ignores retired callbacks", async () => {
  const container = await render(
    <CommandSelection threadId="thread" sourceId="source" projectionEpoch="epoch" commandIds={["command"]}>
      {(rows) => <p>{rows.length} commands retained</p>}
    </CommandSelection>
  );
  const retired = option("agentplane-commands:");

  await act(async () => retired.shapeOptions.onError?.(new Error("stream closed after ready")));

  expect(container.textContent).toContain("Command synchronization stopped: stream closed after ready");
  expect(container.textContent).toContain("0 commands retained");
  await act(async () => (container.querySelector("button") as HTMLButtonElement).click());
  expect(captured.options.filter((value) => value.id.startsWith("agentplane-commands:")).length).toBe(2);

  await rerender(
    <CommandSelection threadId="thread" sourceId="source" projectionEpoch="epoch" commandIds={["other-command"]}>
      {(rows) => <p>{rows.length} commands retained</p>}
    </CommandSelection>
  );
  expect(container.textContent).not.toContain("stream closed after ready");

  await act(async () => retired.shapeOptions.onError?.(new Error("retired callback")));
  expect(container.textContent).not.toContain("retired callback");
});

it("keeps a payload callback error through a follow revision and replaces it on retry", async () => {
  const fetch = vi.fn().mockResolvedValue(Response.json({ chunk_count: "1", content_bytes: "4" }));
  vi.stubGlobal("fetch", fetch);
  const reference: PayloadRef = {
    source_id: "source",
    projection_epoch: "epoch",
    owner_cursor: "1",
    owner_item_id: "item",
    field: "text",
    generation: "1",
    revision_cursor: "1",
  };
  const container = await render(
    <PayloadBody threadId="thread" reference={reference} follow>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  await vi.waitFor(() =>
    expect(captured.options.some((value) => value.id.startsWith("agentplane-payload:"))).toBe(true)
  );
  expect(fetch).toHaveBeenCalledTimes(1);
  await rerender(
    <PayloadBody threadId="thread" reference={{ ...reference }} follow>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  expect(fetch).toHaveBeenCalledTimes(1);
  const retired = option("agentplane-payload:");

  await act(async () => retired.shapeOptions.onError?.(new Error("stream closed after ready")));

  expect(container.textContent).toContain("Payload synchronization stopped: stream closed after ready");
  expect(container.textContent).toContain("payload retained");

  await rerender(
    <PayloadBody threadId="thread" reference={{ ...reference, revision_cursor: "2" }} follow={true}>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  expect(container.textContent).toContain("Payload synchronization stopped: stream closed after ready");
  expect(captured.options.filter((value) => value.id.startsWith("agentplane-payload:")).length).toBe(1);

  await act(async () => (container.querySelector("button") as HTMLButtonElement).click());
  await vi.waitFor(() =>
    expect(captured.options.filter((value) => value.id.startsWith("agentplane-payload:")).length).toBe(2)
  );

  await act(async () => retired.shapeOptions.onError?.(new Error("retired callback")));
  expect(container.textContent).not.toContain("retired callback");
});

it("opens at most one current and one older fixed-ID pending page", async () => {
  const current = Array.from({ length: 30 }, (_, index) => `current-${index}`);
  const older = Array.from({ length: 30 }, (_, index) => `older-${index}`);
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json({
        source_id: "source",
        projection_epoch: "epoch",
        through_cursor: "12",
        command_revision_cursor: "12",
        unresolved_count: 61,
        command_ids: current,
        next_before_cursor: "500",
      })
    )
    .mockResolvedValueOnce(
      Response.json({
        source_id: "source",
        projection_epoch: "epoch",
        through_cursor: "12",
        command_revision_cursor: "12",
        unresolved_count: 61,
        command_ids: older,
        next_before_cursor: "400",
      })
    );
  vi.stubGlobal("fetch", fetch);
  const container = await render(
    <PendingCommandPages
      threadId="thread"
      sourceId="source"
      projectionEpoch="epoch"
      viewRevisionCursor="12"
      commandRevisionCursor="12"
    >
      {(page) => (
        <>
          <p>{page.unresolvedCount} pending</p>
          <p>
            {page.current.length} current and {page.older.length} older
          </p>
          <button onClick={page.loadOlder} disabled={!page.canLoadOlder}>
            Load older
          </button>
        </>
      )}
    </PendingCommandPages>
  );

  await vi.waitFor(() => expect(container.textContent).toContain("61 pending"));
  expect(captured.options.filter((value) => value.id.startsWith("agentplane-commands:"))).toHaveLength(1);
  expect(new URL(fetch.mock.calls[0]![0] as string).pathname).toBe("/threads/thread/sync/pending-interest");

  await act(async () => (container.querySelector("button") as HTMLButtonElement).click());
  await vi.waitFor(() =>
    expect(captured.options.filter((value) => value.id.startsWith("agentplane-commands:"))).toHaveLength(2)
  );
  expect(new URL(fetch.mock.calls[1]![0] as string).searchParams.get("before_cursor")).toBe("500");
  expect(container.textContent).toContain("0 current and 0 older");
});
