/**
 * The harness's stand-in for the network, installed at import time. `openapi-fetch` captures
 * `globalThis.fetch` when `client.ts` creates its client, which happens as the app is imported, so
 * this module must be imported before the app: the harness lists it first. Routes are registered
 * afterwards, since a request cannot arrive before the app has mounted. The ledger is what
 * visual-test-lib's `assertNetworkSettled` reads.
 */

/** An answer is a JSON body, `undefined` for 404, or a ready `Response` for any other status. */
export type Route = [
  method: string,
  pattern: RegExp,
  answer: (match: RegExpMatchArray, query: URLSearchParams, signal: AbortSignal | undefined) => unknown,
];

export const routes: Route[] = [];

/** A real Electric HTTP shape batch: row operations followed by a completed-snapshot control. */
export interface ElectricShapeMessage {
  headers:
    | { relation: ["public", string]; operation: "insert" | "update" | "delete"; snapshot_mark?: number }
    | { control: "snapshot-end" | "up-to-date" | "must-refetch" };
  key?: string;
  value?: Record<string, unknown>;
}

const ELECTRIC_SCHEMAS: Record<string, Record<string, Record<string, string | boolean | number>>> = {
  thread_entity: {
    arguments_ref: { type: "jsonb" },
    cursor: { type: "int8", not_null: true },
    entity_id: { type: "text", not_null: true, pk_index: 3 },
    entity_kind: { type: "text", not_null: true, pk_index: 2 },
    input_ref: { type: "jsonb" },
    output_ref: { type: "jsonb" },
    pending: { type: "bool", not_null: true },
    projection_epoch: { type: "text", not_null: true, pk_index: 1 },
    revision_cursor: { type: "int8", not_null: true },
    state: { type: "jsonb", not_null: true },
    text_ref: { type: "jsonb" },
    thread_id: { type: "uuid", not_null: true, pk_index: 0 },
    turn_id: { type: "text" },
  },
  thread_payload_chunk: {
    chunk_index: { type: "int8", not_null: true, pk_index: 6 },
    field: { type: "text", not_null: true, pk_index: 4 },
    generation: { type: "int8", not_null: true, pk_index: 5 },
    owner_cursor: { type: "int8", not_null: true, pk_index: 2 },
    owner_id: { type: "text", not_null: true, pk_index: 3 },
    projection_epoch: { type: "text", not_null: true, pk_index: 1 },
    text: { type: "text", not_null: true },
    thread_id: { type: "uuid", not_null: true, pk_index: 0 },
  },
};

/**
 * Build the same JSON and protocol headers consumed by `electricCollectionOptions` in production.
 * Visual thread scenes use this rather than an EventSource replay so the collection's column
 * mapping, typed rows, and catch-up boundary are exercised by the browser bundle.
 */
function relationSchema(rows: readonly ElectricShapeMessage[], fallback = "thread_entity") {
  const relation = rows[0]?.headers && "relation" in rows[0].headers ? rows[0].headers.relation[1] : fallback;
  const schema = ELECTRIC_SCHEMAS[relation];
  if (schema === undefined) throw new Error(`no Electric schema for ${relation}`);
  return schema;
}

function shapeHeaders(handle: string, schema: Record<string, Record<string, string | boolean | number>>): HeadersInit {
  return {
    "content-type": "application/json",
    "electric-cursor": "1674440",
    "electric-handle": handle,
    "electric-offset": "0_0",
    "electric-schema": JSON.stringify(schema),
    "electric-has-data": "true",
  };
}

/** Full-log response for immutable chunk shapes and changes-only stream continuations. */
export function electricShape(rows: readonly ElectricShapeMessage[], handle: string, relation?: string): Response {
  const schema = relationSchema(rows, relation);
  return new Response(
    JSON.stringify([...rows, { headers: { control: "snapshot-end" } }, { headers: { control: "up-to-date" } }]),
    {
      headers: {
        ...shapeHeaders(handle, schema),
        "electric-up-to-date": "",
      },
    }
  );
}

/**
 * A valid live response whose body has not received a change yet. Fetch itself settles, while
 * Electric's body reader waits until the collection cancels it during teardown.
 */
export function electricLongPoll(handle: string, relation?: string, signal?: AbortSignal): Response {
  const schema = ELECTRIC_SCHEMAS[relation ?? "thread_entity"];
  if (schema === undefined) throw new Error(`no Electric schema for ${relation}`);
  let onAbort: (() => void) | undefined;
  const removeAbortListener = () => {
    if (onAbort !== undefined) signal?.removeEventListener("abort", onAbort);
    onAbort = undefined;
  };
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      if (signal === undefined) return;
      onAbort = () => {
        removeAbortListener();
        controller.error(new DOMException("The live Shape request was aborted", "AbortError"));
      };
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    },
    cancel() {
      removeAbortListener();
    },
  });
  return new Response(body, { headers: shapeHeaders(handle, schema) });
}

/**
 * Current-state bootstrap used by `syncMode: "on-demand"`: Electric returns operations in a
 * subset envelope rather than the append-only shape log. The snapshot mark links each row to the
 * PostgreSQL visibility metadata and lets the client discard overlapping streamed changes.
 */
export function electricSubset(rows: readonly ElectricShapeMessage[], handle: string): Response {
  const schema = relationSchema(rows);
  const snapshotMark = 974_778_392;
  const data = rows.map((row) =>
    "relation" in row.headers ? { ...row, headers: { ...row.headers, snapshot_mark: snapshotMark } } : row
  );
  return new Response(
    JSON.stringify({
      data,
      metadata: {
        snapshot_mark: snapshotMark,
        database_lsn: "25413256",
        xip_list: [],
        xmax: "761",
        xmin: "761",
      },
    }),
    {
      headers: {
        ...shapeHeaders(handle, schema),
        "electric-offset": "0_inf",
        "electric-snapshot": "true",
      },
    }
  );
}

interface Ledger {
  pending: string[];
  violations: string[];
}

const ledger: Ledger = { pending: [], violations: [] };
(window as unknown as { __visualNetworkLedger__: Ledger }).__visualNetworkLedger__ = ledger;

window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = new URL(
    typeof input === "string" ? input : input instanceof Request ? input.url : input.href,
    "http://harness"
  );
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
  const key = `${method} ${url.pathname}${url.search}`;
  ledger.pending.push(key);
  try {
    for (const [routeMethod, pattern, answer] of routes) {
      const match = url.pathname.match(pattern);
      if (routeMethod !== method || !match) continue;
      const body = answer(match, url.searchParams, signal);
      if (body instanceof Response) return body;
      if (body === undefined) return Response.json({ detail: `no such sandbox ${match[1]}` }, { status: 404 });
      return Response.json(body);
    }
    ledger.violations.push(`unmatched ${key}`);
    return Response.json({ detail: "not in the harness" }, { status: 503 });
  } finally {
    ledger.pending.splice(ledger.pending.indexOf(key), 1);
  }
};
