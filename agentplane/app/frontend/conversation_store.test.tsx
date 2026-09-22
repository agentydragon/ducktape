// @vitest-environment happy-dom

import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

type CapturedOptions = { id: string; shapeOptions: { url: string; onError?: (reason: unknown) => void } };

const captured = vi.hoisted((): { options: CapturedOptions[] } => ({ options: [] }));

vi.mock("@tanstack/electric-db-collection", () => ({
  electricCollectionOptions: (options: CapturedOptions) => {
    captured.options.push(options);
    return options;
  },
}));

vi.mock("@tanstack/react-db", () => ({
  createCollection: <T,>(options: T) => options,
  useLiveQuery: () => ({ data: [], isError: false }),
}));

import { FetchError } from "@electric-sql/client";

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

it("keeps a body on screen through a stream error and rebuilds its shape on retry", async () => {
  const reference: PayloadRef = {
    source_id: "source",
    projection_epoch: "epoch",
    owner_cursor: "1",
    owner_item_id: "item",
    field: "output",
    generation: "1",
    revision_cursor: "1",
    content_bytes: "4",
  };
  const container = await render(
    <PayloadBody threadId="thread" reference={reference}>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  await vi.waitFor(() =>
    expect(captured.options.some((value) => value.id.startsWith("agentplane-payload:"))).toBe(true)
  );
  // A body outside the window shape reads on its own, and its shape names a generation and no
  // revision within it: a re-render at a later revision of the same generation reuses it.
  await rerender(
    <PayloadBody threadId="thread" reference={{ ...reference, revision_cursor: "2", content_bytes: "8" }}>
      {(body) => <p>{body ?? "payload retained"}</p>}
    </PayloadBody>
  );
  expect(captured.options.filter((value) => value.id.startsWith("agentplane-payload:")).length).toBe(1);
  const retired = option("agentplane-payload:");

  await act(async () => retired.shapeOptions.onError?.(new Error("stream closed after ready")));

  expect(container.textContent).toContain("Payload synchronization stopped: stream closed after ready");
  expect(container.textContent).toContain("payload retained");

  await act(async () => (container.querySelector("button") as HTMLButtonElement).click());
  await vi.waitFor(() =>
    expect(captured.options.filter((value) => value.id.startsWith("agentplane-payload:")).length).toBe(2)
  );

  await act(async () => retired.shapeOptions.onError?.(new Error("retired callback")));
  expect(container.textContent).not.toContain("retired callback");
});

it("opens the window's content shape under the same bounds as its entities", async () => {
  const interest = {
    source_id: "source",
    projection_epoch: "epoch",
    anchor_cursor: "40",
    tail_from: "11",
    window_from: "3",
    window_before: "9",
    through_cursor: "40",
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(interest)));
  await render(<ConversationCollection threadId="thread">{() => <p>retained conversation</p>}</ConversationCollection>);
  await vi.waitFor(() =>
    expect(captured.options.some((value) => value.id.startsWith("agentplane-content:"))).toBe(true)
  );

  // Whatever the server admitted as this reader's window bounds both shapes: a body outside the
  // entities a window carries would be a body with nothing to render it.
  const bounds = (id: string): string => {
    const url = new URL(option(id).shapeOptions.url);
    return ["source_id", "projection_epoch", "anchor_cursor", "tail_from", "window_from", "window_before"]
      .map((key) => `${key}=${url.searchParams.get(key)}`)
      .join("&");
  };
  expect(bounds("agentplane-content:")).toBe(bounds("agentplane-conversation:"));
  expect(bounds("agentplane-content:")).toContain("tail_from=11");
  expect(bounds("agentplane-content:")).toContain("window_before=9");
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
