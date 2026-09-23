// @vitest-environment happy-dom

/**
 * The store over Electric's real `ShapeStream`, against a fake of the sync routes behind `fetch`:
 * what these pin is which requests the store makes and what it keeps of the answers.
 */
import { act, type JSX, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { CommandSelection, PayloadBody, ThreadCollection, type ThreadEntity, type ThreadHistory } from "./thread_store";

type Json = Record<string, unknown>;

const SCHEMAS: Record<string, Json> = {
  thread_entity: {
    thread_id: { type: "uuid", not_null: true, pk_index: 0 },
    projection_epoch: { type: "text", not_null: true, pk_index: 1 },
    entity_kind: { type: "text", not_null: true, pk_index: 2 },
    entity_id: { type: "text", not_null: true, pk_index: 3 },
    entity_index: { type: "int8", not_null: true },
    cursor: { type: "int8", not_null: true },
    revision_cursor: { type: "int8", not_null: true },
    pending: { type: "bool", not_null: true },
    turn_id: { type: "text" },
    state: { type: "jsonb", not_null: true },
    text_ref: { type: "jsonb" },
    arguments_ref: { type: "jsonb" },
    output_ref: { type: "jsonb" },
    input_ref: { type: "jsonb" },
  },
  thread_payload_chunk: {
    thread_id: { type: "uuid", not_null: true, pk_index: 0 },
    projection_epoch: { type: "text", not_null: true, pk_index: 1 },
    owner_cursor: { type: "int8", not_null: true, pk_index: 2 },
    owner_id: { type: "text", not_null: true, pk_index: 3 },
    field: { type: "text", not_null: true, pk_index: 4 },
    generation: { type: "int8", not_null: true, pk_index: 5 },
    chunk_index: { type: "int8", not_null: true, pk_index: 6 },
    text: { type: "text", not_null: true },
  },
};

function key(relation: string, row: Json): string {
  const schema = SCHEMAS[relation] as Record<string, { pk_index?: number }>;
  const identity = Object.entries(schema)
    .filter(([, column]) => column.pk_index !== undefined)
    .map(([name]) => JSON.stringify(String(row[name])));
  return `"public"."${relation}"/${identity.join("/")}`;
}

function change(relation: string, operation: "insert" | "update" | "delete", row: Json): Json {
  return { key: key(relation, row), value: row, headers: { relation: ["public", relation], operation } };
}

function headers(relation: string, handle: string): HeadersInit {
  return {
    "content-type": "application/json",
    "electric-handle": handle,
    "electric-offset": "0_0",
    "electric-cursor": "1",
    "electric-schema": JSON.stringify(SCHEMAS[relation]),
  };
}

function log(relation: string, handle: string, messages: Json[]): Response {
  return new Response(JSON.stringify([...messages, { headers: { control: "up-to-date" } }]), {
    headers: { ...headers(relation, handle), "electric-up-to-date": "" },
  });
}

function item(index: number, epoch = "epoch-1", extra: Json = {}): Json {
  return {
    thread_id: "thread",
    projection_epoch: epoch,
    entity_kind: "item",
    entity_id: `item-${index}`,
    entity_index: String(index),
    cursor: String(index),
    revision_cursor: String(index),
    pending: "false",
    turn_id: "turn",
    state: JSON.stringify({ kind: 1, tool_name: "", completion: null, tool_succeeded: null }),
    text_ref: null,
    arguments_ref: null,
    output_ref: null,
    input_ref: null,
    ...extra,
  };
}

function viewState(through: string, epoch = "epoch-1"): Json {
  return {
    ...item(0, epoch),
    entity_kind: "view_state",
    entity_id: "current",
    revision_cursor: through,
    state: JSON.stringify({
      controls: { applied_model: null, active_turn_id: null, harness_state: null },
      operational: { status: "active", last_verified_cursor: through, feed_error: null },
    }),
  };
}

function command(id: string, index: number): Json {
  return {
    ...item(index),
    entity_kind: "command",
    entity_id: id,
    state: JSON.stringify({ operation: "submit_input", outcome: "failed", outcome_cursor: "9", outcome_reason: null }),
  };
}

function textRef(owner: string, chunks: number): string {
  return JSON.stringify({
    projection_epoch: "epoch-1",
    owner_cursor: "1",
    owner_id: owner,
    field: "text",
    revision_cursor: "1",
    generation: "1",
    chunk_count: String(chunks),
  });
}

function chunk(owner: string, index: number, text: string): Json {
  return {
    thread_id: "thread",
    projection_epoch: "epoch-1",
    owner_cursor: "1",
    owner_id: owner,
    field: "text",
    generation: "1",
    chunk_index: String(index),
    text,
  };
}

interface Subset {
  where?: string;
  params?: Record<string, string>;
  order_by?: string;
  limit?: number;
}

/** The proxy's routes over in-memory rows, one Electric shape per path. */
class FakeSync {
  epoch = "epoch-1";
  through = "70";
  entities: Json[] = [];
  chunks: Json[] = [];
  readonly requests: { method: string; path: string; query: URLSearchParams; subset: Subset | null }[] = [];
  readonly #live = new Map<string, (response: Response) => void>();

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const method = init?.method ?? "GET";
    const subset = typeof init?.body === "string" ? (JSON.parse(init.body) as Subset) : null;
    const path = url.pathname.split("/sync/")[1];
    this.requests.push({ method, path, query: url.searchParams, subset });
    if (path === "scope") return Response.json({ projection_epoch: this.epoch, through_cursor: this.through });
    if (url.searchParams.get("projection_epoch") !== this.epoch) return Response.json({}, { status: 410 });
    const relation = path === "entities" ? "thread_entity" : "thread_payload_chunk";
    // Electric resolves a shape's handle from its definition.
    const handle = url.searchParams.get("handle") ?? `${path}-1`;
    if (subset !== null) {
      const rows = relation === "thread_entity" ? this.#entitySubset(subset) : this.#bodySubset(subset);
      return new Response(
        JSON.stringify({
          data: rows.map((row) => change(relation, "insert", row)),
          metadata: { snapshot_mark: 1, database_lsn: "1", xip_list: [], xmin: "1", xmax: "1" },
        }),
        { headers: headers(relation, handle) }
      );
    }
    if (url.searchParams.get("live") !== "true") return log(relation, handle, []);
    return new Promise((resolve, reject) => {
      this.#live.set(path, resolve);
      init?.signal?.addEventListener("abort", () => {
        this.#live.delete(path);
        reject(new DOMException("aborted", "AbortError"));
      });
    });
  };

  /** Answer the shape's waiting live request. */
  async respond(path: string, response: (relation: string) => Response): Promise<void> {
    await vi.waitFor(() => expect(this.#live.has(path)).toBe(true));
    const resolve = this.#live.get(path)!;
    this.#live.delete(path);
    await act(async () => resolve(response(path === "entities" ? "thread_entity" : "thread_payload_chunk")));
  }

  posted(path: string): Subset[] {
    return this.requests
      .filter((request) => request.method === "POST" && request.path === path)
      .map((request) => request.subset!);
  }

  #entitySubset(subset: Subset): Json[] {
    const index = (row: Json) => Number(row.entity_index);
    const newestFirst = (rows: Json[]) => [...rows].sort((left, right) => index(right) - index(left));
    const bound = Number(subset.params?.["1"]);
    const rows =
      subset.where === undefined
        ? newestFirst(this.entities)
        : subset.where === "entity_index < $1"
          ? newestFirst(this.entities.filter((row) => index(row) < bound))
          : subset.where === "entity_kind = 'view_state'"
            ? this.entities.filter((row) => row.entity_kind === "view_state")
            : subset.where === "entity_kind = 'command' AND pending = true"
              ? this.entities.filter((row) => row.entity_kind === "command" && row.pending === "true")
              : this.entities.filter(
                  (row) =>
                    row.entity_kind === "command" &&
                    (JSON.parse(`[${subset.params!["1"].slice(1, -1)}]`) as string[]).includes(String(row.entity_id))
                );
    return rows.slice(0, subset.limit);
  }

  #bodySubset(subset: Subset): Json[] {
    const params = subset.params ?? {};
    const bodies = Array.from({ length: Object.keys(params).length / 2 }, (_, index) => [
      params[String(2 * index + 1)],
      params[String(2 * index + 2)],
    ]);
    return this.chunks.filter((row) =>
      bodies.some(([owner, generation]) => row.owner_id === owner && row.generation === generation)
    );
  }
}

let root: ReturnType<typeof createRoot> | undefined;

async function render(element: ReactNode): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(element));
  return container;
}

function stubSync(): FakeSync {
  const sync = new FakeSync();
  vi.stubGlobal("fetch", sync.fetch);
  return sync;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

function Rows({ rows, history }: { rows: ThreadEntity[]; history: ThreadHistory }): JSX.Element {
  const items = rows.filter((row) => row.entityKind === "item");
  return (
    <>
      <p data-testid="items">{items.map((row) => `${row.entityId}@${row.revisionCursor.toString()}`).join(" ")}</p>
      <button disabled={!history.olderAvailable} onClick={history.loadOlder}>
        older
      </button>
    </>
  );
}

function itemsShown(container: HTMLElement): string[] {
  return (container.querySelector('[data-testid="items"]')?.textContent ?? "").split(" ").filter(Boolean);
}

function thread(sync: FakeSync, count: number, epoch = "epoch-1"): void {
  sync.entities = [viewState(sync.through, epoch), ...Array.from({ length: count }, (_, n) => item(n + 1, epoch))];
}

it("opens one shape on the tail and pages older rows into it", async () => {
  const sync = stubSync();
  thread(sync, 70);
  const container = await render(
    <ThreadCollection threadId="thread">{(rows, history) => <Rows rows={rows} history={history} />}</ThreadCollection>
  );
  await vi.waitFor(() => expect(itemsShown(container)).toHaveLength(30));
  expect(itemsShown(container)).toContain("item-41@41");

  const older = container.querySelector("button")!;
  await act(async () => older.click());
  await vi.waitFor(() => expect(itemsShown(container)).toHaveLength(60));
  await act(async () => older.click());
  await vi.waitFor(() => expect(itemsShown(container)).toHaveLength(70));
  expect(older.disabled).toBe(true);

  // The proxy admits these forms exactly.
  expect(sync.posted("entities")).toContainEqual({
    where: "entity_index < $1",
    params: { "1": "41" },
    order_by: "entity_index DESC",
    limit: 30,
  });
  // One shape: every request after the first names its handle.
  expect(new Set(sync.requests.map((request) => request.path))).toEqual(new Set(["scope", "entities"]));
  expect(new Set(sync.requests.map((request) => request.query.get("handle")).filter(Boolean))).toEqual(
    new Set(["entities-1"])
  );
});

it("applies live changes to the rows it holds, and new rows, but not rows it has not loaded", async () => {
  const sync = stubSync();
  thread(sync, 70);
  const container = await render(
    <ThreadCollection threadId="thread">{(rows, history) => <Rows rows={rows} history={history} />}</ThreadCollection>
  );
  await vi.waitFor(() => expect(itemsShown(container)).toHaveLength(30));

  await sync.respond("entities", (relation) =>
    log(relation, "entities-1", [
      change(relation, "update", item(70, "epoch-1", { revision_cursor: "71" })),
      change(relation, "update", item(5, "epoch-1", { revision_cursor: "72" })),
      change(relation, "insert", item(71)),
    ])
  );

  await vi.waitFor(() => expect(itemsShown(container)).toContain("item-71@71"));
  expect(itemsShown(container)).toContain("item-70@71");
  expect(itemsShown(container).some((shown) => shown.startsWith("item-5@"))).toBe(false);
});

it("re-reads a retired epoch's scope and swaps windows under the same children", async () => {
  const sync = stubSync();
  thread(sync, 3);
  const container = await render(
    <ThreadCollection threadId="thread">
      {(rows, history) => (
        <>
          <input aria-label="draft" />
          <Rows rows={rows} history={history} />
        </>
      )}
    </ThreadCollection>
  );
  await vi.waitFor(() => expect(itemsShown(container)).toHaveLength(3));
  const draft = container.querySelector("input")!;

  sync.epoch = "epoch-2";
  thread(sync, 4, "epoch-2");
  await sync.respond("entities", () => Response.json({}, { status: 410 }));

  await vi.waitFor(() => expect(itemsShown(container)).toHaveLength(4));
  expect(container.querySelector("input")).toBe(draft);
  expect(sync.requests.filter((request) => request.path === "scope")).toHaveLength(2);
});

it("reloads as many rows as it held when Electric retires the shape's log, dropping deleted ones", async () => {
  const sync = stubSync();
  thread(sync, 70);
  const container = await render(
    <ThreadCollection threadId="thread">{(rows, history) => <Rows rows={rows} history={history} />}</ThreadCollection>
  );
  await vi.waitFor(() => expect(itemsShown(container)).toHaveLength(30));
  await act(async () => container.querySelector("button")!.click());
  await vi.waitFor(() => expect(itemsShown(container)).toHaveLength(60));

  sync.entities = sync.entities.filter((row) => row.entity_id !== "item-50");
  await sync.respond("entities", (relation) =>
    Response.json([], { status: 409, headers: { ...headers(relation, "entities-2") } })
  );

  await vi.waitFor(() => expect(itemsShown(container)).not.toContain("item-50@50"));
  expect(itemsShown(container)).toHaveLength(60);
  expect(itemsShown(container)).toContain("item-10@10");
});

it("loads the bodies in view in one read, as far as each reference spans, and follows appends", async () => {
  const sync = stubSync();
  sync.through = "2";
  sync.entities = [
    viewState("2"),
    item(1, "epoch-1", { text_ref: textRef("a", 2) }),
    item(2, "epoch-1", { text_ref: textRef("b", 1) }),
  ];
  sync.chunks = [chunk("a", 0, "Hel"), chunk("a", 1, "lo"), chunk("a", 2, " there"), chunk("b", 0, "Bye")];
  const container = await render(
    <ThreadCollection threadId="thread">
      {(rows) => (
        <>
          {rows.map((row) =>
            row.textRef ? (
              <PayloadBody key={row.entityId} reference={row.textRef}>
                {(body) => <p data-body={row.entityId}>{body ?? "loading"}</p>}
              </PayloadBody>
            ) : null
          )}
        </>
      )}
    </ThreadCollection>
  );
  const body = (id: string) => container.querySelector(`[data-body="${id}"]`)?.textContent;
  await vi.waitFor(() => expect([body("item-1"), body("item-2")]).toEqual(["Hello", "Bye"]));
  const [read, ...others] = sync.posted("chunks/text");
  expect(others).toEqual([]);
  expect(read.where).toBe("(owner_id = $1 AND generation = $2) OR (owner_id = $3 AND generation = $4)");
  const params = read.params!;
  expect(new Set([`${params["1"]}@${params["2"]}`, `${params["3"]}@${params["4"]}`])).toEqual(new Set(["a@1", "b@1"]));

  await sync.respond("chunks/text", (relation) =>
    log(relation, "chunks/text-1", [change(relation, "insert", chunk("a", 3, "!"))])
  );
  await sync.respond("entities", (relation) =>
    log(relation, "entities-1", [change(relation, "update", item(1, "epoch-1", { text_ref: textRef("a", 4) }))])
  );
  await vi.waitFor(() => expect(body("item-1")).toBe("Hello there!"));
});

it("loads commands by id as a quoted array", async () => {
  const sync = stubSync();
  // Older than the tail: only a read by id loads them.
  thread(sync, 40);
  sync.entities.push(command('say "hi"', 41), command("other", 42));
  for (const row of sync.entities)
    if (row.entity_kind === "item") row.entity_index = String(Number(row.entity_index) + 2);
  sync.entities.find((row) => row.entity_id === 'say "hi"')!.entity_index = "1";
  sync.entities.find((row) => row.entity_id === "other")!.entity_index = "2";
  const container = await render(
    <ThreadCollection threadId="thread">
      {() => (
        <CommandSelection commandIds={['say "hi"', "missing"]}>
          {(rows) => <p data-testid="commands">{rows.map((row) => row.entityId).join(",")}</p>}
        </CommandSelection>
      )}
    </ThreadCollection>
  );
  await vi.waitFor(() => expect(container.querySelector('[data-testid="commands"]')?.textContent).toBe('say "hi"'));
  expect(sync.posted("entities")).toContainEqual({
    where: "entity_kind = 'command' AND entity_id = ANY($1)",
    params: { "1": '{"missing","say \\"hi\\""}' },
    order_by: "entity_index DESC",
    limit: 2,
  });
});
