/** Electric Shape, event stream and watch responses for the app harness. */

import { ELECTRIC_SCHEMAS } from "../sessions/testing/electric_entity";

/** A real Electric HTTP shape batch: row operations followed by a completed-snapshot control. */
export interface ElectricShapeMessage {
  headers:
    | { relation: ["public", string]; operation: "insert" | "update" | "delete"; snapshot_mark?: number }
    | { control: "snapshot-end" | "up-to-date" | "must-refetch" };
  key?: string;
  value?: Record<string, unknown>;
}

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

function shapeHeaders(handle: string, schema: (typeof ELECTRIC_SCHEMAS)[string]): HeadersInit {
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
  signal: AbortSignal | undefined,
  drop = false
): Response {
  return streamResponse(
    "application/x-ndjson",
    ndjson(initial),
    signal,
    drop ? null : { path, updates: () => ndjson(updates()) },
    drop
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
  let dropping = drop;
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
    // Called once the reader has taken what `start` sent. Erroring discards what the reader's pipe
    // stages still hold, so the drop waits a task for them to hand those events on.
    pull(controller) {
      if (!dropping) return;
      dropping = false;
      setTimeout(() => {
        dispose();
        controller.error(new TypeError("the harness dropped this stream"));
      }, 0);
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
