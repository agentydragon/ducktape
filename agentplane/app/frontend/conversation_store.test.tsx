// @vitest-environment happy-dom

import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

const captured = vi.hoisted(
  (): {
    options: Array<{ id: string; shapeOptions: { onError?: (reason: unknown) => void } }>;
    rows: unknown[];
  } => ({ options: [], rows: [] })
);

vi.mock("@tanstack/electric-db-collection", () => ({
  electricCollectionOptions: (options: { id: string; shapeOptions: { onError?: (reason: unknown) => void } }) => {
    captured.options.push(options);
    return options;
  },
}));

vi.mock("@tanstack/react-db", () => ({
  createCollection: <T,>(options: T) => options,
  useLiveQuery: () => ({ data: captured.rows, isError: false }),
}));

import { FetchError } from "@electric-sql/client";

import { api } from "./client";
import { CommandSelection, ConversationCollection, PayloadBody, type PayloadRef } from "./conversation_store";

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
  captured.rows.splice(0);
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

const REFERENCE: PayloadRef = {
  source_id: "source",
  projection_epoch: "epoch",
  owner_cursor: "1",
  owner_item_id: "item",
  field: "text",
  generation: "1",
  revision_cursor: "1",
};

function chunk(index: number, text: string) {
  return {
    threadId: "thread",
    sourceId: "source",
    projectionEpoch: "epoch",
    ownerCursor: "1",
    ownerId: "item",
    field: "text",
    generation: "1",
    chunkIndex: String(index),
    text,
  };
}

it("follows one shape across a streaming generation's revisions, with no request per revision", async () => {
  const get = vi.spyOn(api, "GET");
  captured.rows.push(chunk(0, "Hi! "));
  const container = await render(
    <PayloadBody threadId="thread" reference={REFERENCE} follow>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  expect(container.textContent).toContain("Hi! ");

  await rerender(
    <PayloadBody threadId="thread" reference={{ ...REFERENCE, revision_cursor: "2" }} follow>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );

  // An advancing revision within a generation appends; redefining the shape for it would make every
  // delta pay a shape creation for chunks the follow stream already delivered.
  expect(captured.options.filter((value) => value.id.startsWith("agentplane-payload:")).length).toBe(1);
  expect(get).not.toHaveBeenCalled();
});

it("renders the contiguous chunk prefix and stops at a gap", async () => {
  captured.rows.push(chunk(0, "Hi! "), chunk(1, "How can I help?"), chunk(3, "orphaned"));
  const container = await render(
    <PayloadBody threadId="thread" reference={REFERENCE} follow>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  expect(container.textContent).toContain("Hi! How can I help?");
  expect(container.textContent).not.toContain("orphaned");
});

it("keeps a payload callback error until the reader retries the stream", async () => {
  const container = await render(
    <PayloadBody threadId="thread" reference={REFERENCE} follow>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  const retired = option("agentplane-payload:");

  await act(async () => retired.shapeOptions.onError?.(new Error("stream closed after ready")));
  expect(container.textContent).toContain("Payload synchronization stopped: stream closed after ready");

  await act(async () => (container.querySelector("button") as HTMLButtonElement).click());
  await vi.waitFor(() =>
    expect(captured.options.filter((value) => value.id.startsWith("agentplane-payload:")).length).toBe(2)
  );

  await act(async () => retired.shapeOptions.onError?.(new Error("retired callback")));
  expect(container.textContent).not.toContain("retired callback");
});

it("reads a completed revision over HTTP and holds the streamed text through the handoff", async () => {
  let settle: (body: string) => void = () => undefined;
  const get = vi.spyOn(api, "GET").mockImplementation(
    async () =>
      new Promise((resolve) => {
        settle = (body) =>
          resolve({ data: { availability: "present", body }, response: new Response() } as Awaited<
            ReturnType<typeof api.GET>
          >);
      })
  );
  captured.rows.push(chunk(0, "Hi! How can I help?"));
  const container = await render(
    <PayloadBody threadId="thread" reference={REFERENCE} follow>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  expect(container.textContent).toContain("Hi! How can I help?");

  captured.rows.splice(0);
  await rerender(
    <PayloadBody threadId="thread" reference={{ ...REFERENCE, revision_cursor: "2" }} follow={false}>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );

  // A completed revision is immutable, so it reads whole over HTTP rather than defining a shape —
  // and the text the reader is already reading stays put while that read is in flight.
  expect(captured.options.filter((value) => value.id.startsWith("agentplane-payload:")).length).toBe(1);
  expect(container.textContent).toContain("Hi! How can I help?");
  await vi.waitFor(() =>
    expect(get).toHaveBeenCalledWith("/threads/{thread_id}/conversation/payload", expect.anything())
  );

  await act(async () => settle("Hi! How can I help you today?"));
  await vi.waitFor(() => expect(container.textContent).toContain("Hi! How can I help you today?"));
});

it("refreshes the active conversation selection after a disconnected ready stream", async () => {
  const interest = {
    source_id: "source",
    projection_epoch: "epoch",
    anchor_cursor: "1",
    tail_from: "1",
    window_from: null,
    window_before: null,
    through_cursor: "1",
  };
  const fetch = vi.fn(() => Promise.resolve(Response.json(interest)));
  vi.stubGlobal("fetch", fetch);
  await render(<ConversationCollection threadId="thread">{() => <p>retained conversation</p>}</ConversationCollection>);
  await vi.waitFor(() =>
    expect(captured.options.filter((value) => value.id.startsWith("agentplane-conversation:"))).toHaveLength(1)
  );

  const active = option("agentplane-conversation:");
  await act(async () =>
    active.shapeOptions.onError?.(new FetchError(0, "stream disconnected", undefined, {}, "", "stream disconnected"))
  );
  await vi.waitFor(() =>
    expect(captured.options.filter((value) => value.id.startsWith("agentplane-conversation:"))).toHaveLength(2)
  );
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("keeps native Electric stream errors visible instead of rotating the active conversation", async () => {
  const interest = {
    source_id: "source",
    projection_epoch: "epoch",
    anchor_cursor: "1",
    tail_from: "1",
    window_from: null,
    window_before: null,
    through_cursor: "1",
  };
  const fetch = vi.fn().mockResolvedValue(Response.json(interest));
  vi.stubGlobal("fetch", fetch);
  const container = await render(
    <ConversationCollection threadId="thread">{() => <p>retained conversation</p>}</ConversationCollection>
  );
  await vi.waitFor(() =>
    expect(captured.options.filter((value) => value.id.startsWith("agentplane-conversation:"))).toHaveLength(1)
  );

  const active = option("agentplane-conversation:");
  await act(async () =>
    active.shapeOptions.onError?.(new FetchError(409, "must refetch", undefined, {}, "", "must refetch"))
  );

  expect(container.textContent).toContain("Conversation synchronization stopped: must refetch");
  expect(captured.options.filter((value) => value.id.startsWith("agentplane-conversation:"))).toHaveLength(1);
  expect(fetch).toHaveBeenCalledTimes(1);
});
