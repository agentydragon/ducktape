/**
 * The harness's stand-in for the network, installed at import time. `openapi-fetch` captures
 * `globalThis.fetch` when `client.ts` creates its client, which happens as the app is imported, so
 * this module must be imported before the app: the harness lists it first. Routes are registered
 * afterwards, since a request cannot arrive before the app has mounted. The ledger is what the
 * sweep's `assert_network_settled` reads.
 */

/** An answer is a JSON body, `undefined` for 404, a ready `Response` for any other status, or
 * `UNANSWERED`. */
export type Route = [
  method: string,
  pattern: RegExp,
  answer: (
    match: RegExpMatchArray,
    query: URLSearchParams,
    signal: AbortSignal | undefined,
    body: string | undefined
  ) => unknown,
];

export const routes: Route[] = [];

/**
 * An answer that never comes, as for a request queued behind the browser's connection limit: the
 * fetch stays pending until its signal aborts, and the ledger does not wait for it.
 */
export const UNANSWERED: unique symbol = Symbol("unanswered");

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
    entity_index: { type: "int8", not_null: true },
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
    text: { type: "json", not_null: true },
    thread_id: { type: "uuid", not_null: true, pk_index: 0 },
  },
};

/**
 * Build the same JSON and protocol headers Electric's `ShapeStream` consumes in production, so the
 * thread store's column mapping, typed rows, and catch-up boundary are exercised by the browser
 * bundle.
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
 * A live SSE response that has not carried a change yet. Fetch itself settles, while Electric's
 * event reader waits until the store closes the shape.
 */
export function electricLive(handle: string, relation?: string, signal?: AbortSignal): Response {
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
  return new Response(body, { headers: { ...shapeHeaders(handle, schema), "content-type": "text/event-stream" } });
}

interface PublishedStream {
  path: string;
  publish: () => void;
}

const publishedStreams = new Set<PublishedStream>();

/** Publish current frames to every open stream whose path ends with the suffix. */
export function publishStreams(pathSuffix: string): void {
  for (const stream of publishedStreams) if (stream.path.endsWith(pathSuffix)) stream.publish();
}

function ndjson(results: readonly unknown[]): string {
  return results.map((result) => `${JSON.stringify({ result })}\n`).join("");
}

/** A live facade watch: snapshot frames first, then delta frames when a fixture publishes. */
export function liveNdjsonWatch(
  path: string,
  initial: readonly unknown[],
  updates: () => readonly unknown[],
  signal: AbortSignal | undefined
): Response {
  return streamResponse(
    "application/x-ndjson",
    ndjson(initial),
    signal,
    {
      path,
      updates: () => ndjson(updates()),
    },
    false
  );
}

/** A gRPC-Gateway stream sends `results` and stays open until its reader cancels or signal aborts. */
export function ndjsonWatch(results: readonly unknown[], signal: AbortSignal | undefined): Response {
  return streamResponse("application/x-ndjson", ndjson(results), signal, null, false);
}

/** One server-sent event; `data` goes as JSON. */
export interface ServerSentEvent {
  event: string;
  data: unknown;
}

/**
 * A live event stream: `events()` when it opens and again whenever a fixture publishes its path.
 * With `drop`, the body fails once the first events are read, as on a dropped network.
 */
export function liveEventStream(
  path: string,
  events: () => readonly ServerSentEvent[],
  signal: AbortSignal | undefined,
  drop: boolean
): Response {
  const text = (): string =>
    events()
      .map(({ event, data }) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
      .join("");
  return streamResponse("text/event-stream", text(), signal, drop ? null : { path, updates: text }, drop);
}

function streamResponse(
  contentType: string,
  initial: string,
  signal: AbortSignal | undefined,
  publication: { path: string; updates: () => string } | null,
  drop: boolean
): Response {
  let dispose = (): void => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      const send = (text: string): void => {
        if (text !== "") controller.enqueue(encoder.encode(text));
      };
      const stream =
        publication === null ? null : { path: publication.path, publish: () => send(publication.updates()) };
      const onAbort = (): void => {
        dispose();
        controller.error(new DOMException("The stream request was aborted", "AbortError"));
      };
      dispose = (): void => {
        signal?.removeEventListener("abort", onAbort);
        if (stream !== null) publishedStreams.delete(stream);
      };
      if (signal?.aborted) {
        onAbort();
        return;
      }
      if (stream !== null) publishedStreams.add(stream);
      signal?.addEventListener("abort", onAbort, { once: true });
      send(initial);
    },
    // Called once the reader has taken what `start` sent: erroring sooner would discard it.
    pull(controller) {
      if (!drop) return;
      dispose();
      controller.error(new TypeError("the harness dropped this stream"));
    },
    cancel() {
      dispose();
    },
  });
  return new Response(body, { headers: { "content-type": contentType } });
}

/**
 * A subset snapshot: Electric returns operations in a subset envelope rather than the append-only
 * shape log. The snapshot mark links each row to the PostgreSQL visibility metadata and lets the
 * client discard overlapping streamed changes.
 */
export function electricSubset(rows: readonly ElectricShapeMessage[], handle: string, relation: string): Response {
  const schema = relationSchema(rows, relation);
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
      const requestBody =
        typeof init?.body === "string"
          ? init.body
          : input instanceof Request && input.body !== null
            ? await input.clone().text()
            : undefined;
      const body = answer(match, url.searchParams, signal, requestBody);
      if (body === UNANSWERED) {
        return new Promise<Response>((_resolve, reject) => {
          if (signal) signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        });
      }
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
