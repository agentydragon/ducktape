/**
 * A thread's rows and bodies, synchronized from Electric.
 *
 * A thread is one shape over its rows, and each payload field one shape over its bodies' chunks,
 * both opened from now with `log=changes_only`. The rows a reader holds — the tail, each page it
 * scrolls back to, the view state, the commands it is waiting on, the bodies in view — arrive as
 * subsets of those shapes, and every later change to them on the shapes' live logs. A window
 * therefore moves by loading more, never by opening another shape.
 *
 * A reader who leaves a thread keeps its window, suspended: the shapes' streams close, and the rows
 * and bodies stay with the position in each log they were read through. Coming back opens streams
 * at those positions, so only what changed meanwhile is read.
 *
 * A window also reads the bodies of every row it holds, not only the ones a reader has shown, so
 * scrolling back or opening a disclosure finds them there. Those reads go one batch at a time and
 * after any read a reader is waiting on.
 */
import {
  FetchError,
  ShapeStream,
  isChangeMessage,
  snakeToCamel,
  type ColumnMapper,
  type Message,
  type Offset,
  type Row,
  type SubsetParams,
} from "@electric-sql/client";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState, useSyncExternalStore } from "react";
import { z } from "zod";

import { displayableError, fetchWithLogin, threadScope, type ThreadScope } from "../client";
import type { StreamConnection } from "../live_stream";
import {
  decimalBigInt,
  type Decimal,
  type Payload,
  type PayloadRef,
  type ThreadEntity,
  type ThreadState,
  type ThreadSync,
} from "./thread_sync";

const decimal: z.ZodType<Decimal> = z.union([z.string().regex(/^-?\d+$/), z.bigint()]);
type PayloadField = PayloadRef["field"];

const payloadRefSchema = z.object({
  projection_epoch: z.string(),
  owner_cursor: z.string(),
  owner_id: z.string(),
  field: z.enum(["text", "arguments", "output", "confirmed_input", "command_input"]),
  revision_cursor: z.string(),
  generation: z.string(),
  chunk_count: z.string(),
});

const stateSchema = z.union([
  z.object({
    controls: z.object({
      applied_model: z.string().nullable(),
      applied_reasoning_effort: z.string().nullable(),
      active_turn_id: z.string().nullable(),
      harness_state: z.string().nullable(),
    }),
    operational: z.object({
      status: z.enum(["active", "ended", "failed"]),
      last_verified_cursor: z.string(),
      feed_error: z.object({ cursor: z.string().nullable(), message: z.string() }).nullable(),
    }),
  }),
  z.object({
    kind: z.number().int(),
    tool_name: z.string(),
    completion: z.string().nullable(),
    tool_succeeded: z.boolean().nullable(),
    recovery: z.number().nullable(),
    recovery_reason: z.string(),
  }),
  z.object({ harness_message_id: z.string(), origin_command_ids: z.array(z.string()) }),
  z.object({ observation: z.string(), event: z.unknown() }),
  z.object({
    operation: z.string(),
    outcome: z.enum(["pending", "effected", "failed", "noop"]),
    outcome_cursor: z.string().nullable(),
    outcome_reason: z.string().nullable(),
  }),
]);
const entitySchema = z.object({
  threadId: z.string(),
  projectionEpoch: z.string(),
  entityKind: z.enum(["view_state", "item", "confirmed_input", "lifecycle", "command"]),
  entityId: z.string(),
  entityIndex: decimal,
  cursor: decimal,
  revisionCursor: decimal,
  pending: z.boolean(),
  turnId: z.string().nullable(),
  state: stateSchema,
  textRef: payloadRefSchema.nullable(),
  argumentsRef: payloadRefSchema.nullable(),
  outputRef: payloadRefSchema.nullable(),
  inputRef: payloadRefSchema.nullable(),
});

const chunkSchema = z.object({
  ownerId: z.string(),
  generation: decimal,
  chunkIndex: decimal,
  text: z.string(),
});

// Rows per page: each page before the oldest row a reader holds.
const PAGE = 30;
// The tail a reader opens on: several pages up front, so opening a thread reads like a couple of
// screens of history rather than one short page that immediately asks for another.
const INITIAL_ROWS = PAGE * 3;
// The proxy's bounds on one subset read.
const SUBSET_ROWS = 200;
const SUBSET_BODIES = 100;
// Bodies read ahead of the reader go in reads this size: a body has no size a reader can see before
// reading it, so a read that is not the reader's own stays short.
const AHEAD_BODIES = 20;
// Every entity subset reads positions newest first; Electric requires an order wherever there is a limit.
const NEWEST_FIRST = "entity_index DESC";
// The subset forms the proxy admits, besides the pages and the bodies.
const VIEW_STATE: SubsetParams = { where: "entity_kind = 'view_state'", orderBy: NEWEST_FIRST, limit: 1 };
const PENDING_COMMANDS: SubsetParams = {
  where: "entity_kind = 'command' AND pending = true",
  orderBy: NEWEST_FIRST,
  limit: SUBSET_ROWS,
};

// Subsets are sent as written: the proxy admits them by their exact form, and Electric's client
// would otherwise rewrite what it takes for identifiers, `ANY` among them.
const columns: ColumnMapper = { decode: snakeToCamel, encode: (column) => column };

function batches<T>(values: readonly T[], size: number): T[][] {
  return Array.from({ length: Math.ceil(values.length / size) }, (_, index) =>
    values.slice(index * size, (index + 1) * size)
  );
}

function commandsById(ids: readonly string[]): SubsetParams {
  const array = ids.map((id) => `"${id.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`).join(",");
  return {
    where: "entity_kind = 'command' AND entity_id = ANY($1)",
    params: { "1": `{${array}}` },
    orderBy: NEWEST_FIRST,
    limit: ids.length,
  };
}

interface BodyId {
  ownerId: string;
  generation: string;
}

function bodyKey(ownerId: string, generation: string): string {
  return `${ownerId}\u0000${generation}`;
}

function bodySubset(references: readonly BodyId[]): SubsetParams {
  return {
    where: references
      .map((_, index) => `(owner_id = $${2 * index + 1} AND generation = $${2 * index + 2})`)
      .join(" OR "),
    params: Object.fromEntries(
      references.flatMap((reference, index) => [
        [String(2 * index + 1), reference.ownerId],
        [String(2 * index + 2), reference.generation],
      ])
    ),
  };
}

type Listener = () => void;

class Listeners {
  readonly #listeners = new Set<Listener>();

  subscribe = (listener: Listener): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  notify(): void {
    for (const listener of this.#listeners) listener();
  }
}

/**
 * Keeps a stream where it is in its log when a subset answers. Electric's client moves a stream to
 * a subset response's offset, which is right for a stream with no position yet (`now`) but, for one
 * behind the subset, skips every change in between to rows outside the subset. The stream's own
 * offset is on the request, so the response carries that back instead.
 */
// CLEANUP(added 2026-09-23): Drop, leaving `fetchWithLogin` as the shapes' fetch, once a released
//   @electric-sql/client moves only a stream at `now` to a subset's offset; 1.5.28's requestSnapshot
//   moves a live one too (LiveState.handleResponseMetadata).
async function keepingOffset(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetchWithLogin(input, init);
  const offset = new URL(input instanceof Request ? input.url : String(input)).searchParams.get("offset");
  if (init?.method !== "POST" || !response.ok || offset === null || offset === "now") return response;
  const headers = new Headers(response.headers);
  headers.set("electric-offset", offset);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

/** How far into a shape's log a reader has applied it: a stream opened here reads on from there. */
interface ShapePosition {
  handle: string;
  offset: Offset;
}

/**
 * One Electric shape, from now or from a `ShapePosition`: its rows arrive as subsets of it, and as
 * its live changes, followed over SSE. Electric's client long-polls a shape instead once three SSE
 * responses in a row have ended within a second, as Electric's answers to a reader behind the log
 * do.
 */
class Shape {
  readonly #abort = new AbortController();
  readonly #stream: ShapeStream<Row>;
  readonly #onAttempt: (failure: string | null) => void;
  // Settles with the shape's first subset. Electric answers that one from the shape's definition
  // with the handle and offset the stream then follows; later subsets name that handle. A shape
  // opened at a position already has its handle.
  #opened: Promise<unknown> | null;
  #retrying = false;
  #position: ShapePosition | null;

  constructor(
    url: string,
    onMessages: (messages: Message<Row>[]) => void,
    onError: (error: unknown) => void,
    onAttempt: (failure: string | null) => void,
    from: ShapePosition | null
  ) {
    this.#onAttempt = onAttempt;
    this.#opened = from === null ? null : Promise.resolve();
    this.#position = from;
    this.#stream = new ShapeStream({
      url,
      offset: from?.offset ?? "now",
      handle: from?.handle,
      log: "changes_only",
      liveSse: true,
      subsetMethod: "POST",
      columnMapper: columns,
      fetchClient: (input, init) => this.#attempt(input, init),
      signal: this.#abort.signal,
      onError: (error) => {
        if (!this.#abort.signal.aborted) onError(error);
      },
    });
    this.#stream.subscribe((messages) => {
      onMessages(messages);
      this.#position = messages.some(
        (message) => !isChangeMessage(message) && message.headers.control === "must-refetch"
      )
        ? null
        : this.#reached();
    });
  }

  /**
   * Where the shape's log has been applied through, or null where nothing yet says so. The client
   * moves its offset ahead of the messages it ends up delivering (a long poll's offset is in its
   * headers, read before its body), so the offset is read as each batch is delivered or a subset
   * answers (the stream is held for it, with nothing half-read), not when asked; and a log that was
   * just retired has no position until a batch is delivered on its successor.
   */
  get position(): ShapePosition | null {
    return this.#position;
  }

  #reached(): ShapePosition | null {
    const { shapeHandle: handle, lastOffset: offset } = this.#stream;
    // A stream at `now` or `-1` follows on from the log's end when opened, not from its own.
    return handle === undefined || offset === "now" || offset === "-1" ? null : { handle, offset };
  }

  /**
   * One attempt at one of the stream's requests. Electric's client retries a request that failed on
   * the network or with a 5xx or 429 after a backoff, forever, and calls no `onError` meanwhile
   * (`createFetchWithBackoff`), so the stream is retrying from such a failure until an attempt gets
   * any other answer. Its `onFailedAttempt` hook cannot say so: it takes no argument, and fires as
   * well for a 4xx the client hands back and for a request it aborted itself, as a subset aborts the
   * live read.
   */
  async #attempt(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    let response: Response;
    try {
      response = await keepingOffset(input, init);
    } catch (error) {
      if (!init?.signal?.aborted) this.#attempted(displayableError(error));
      throw error;
    }
    this.#attempted(response.status >= 500 || response.status === 429 ? `HTTP ${response.status}` : null);
    return response;
  }

  /** Every failed attempt, and the first success after them. */
  #attempted(failure: string | null): void {
    // A subset's request carries no signal, so it goes on retrying once the shape is closed.
    if (this.closed || (failure === null && !this.#retrying)) return;
    this.#retrying = failure !== null;
    this.#onAttempt(failure);
  }

  /** Rows of the shape, delivered to its subscriber like any change and returned. */
  async subset(params: SubsetParams): Promise<Row[]> {
    // TRACKED in #8544: Electric 1.5.28 can count a same-offset fetch before requestSnapshot's
    // pause aborts it, so valid subset reads may trip the fast-loop guard. Recheck after an
    // upstream client fix is released.
    const opened = this.#opened;
    const request =
      opened === null ? this.#stream.requestSnapshot(params) : opened.then(() => this.#stream.requestSnapshot(params));
    // A failed first subset reaches its own caller; the ones after it still go.
    this.#opened ??= request.catch(() => undefined);
    const { data } = await request;
    this.#position = this.#reached() ?? this.#position;
    return data.map((message) => message.value);
  }

  get closed(): boolean {
    return this.#abort.signal.aborted;
  }

  close(): void {
    this.#attempted(null);
    this.#abort.abort();
  }
}

/** The bodies of one payload field that a reader shows, from the field's shape. */
class PayloadShape extends Listeners {
  readonly #url: string;
  readonly #onGone: () => void;
  readonly #onAttempt: (failure: string | null) => void;
  #shape: Shape | null = null;
  // Chunk text by index, per body: an owner at one generation.
  readonly #chunks = new Map<string, Map<number, string>>();
  readonly #requested = new Map<string, BodyId>();
  #queued: BodyId[] = [];
  #version = 0;
  #error: string | null = null;
  #closed = false;
  #suspended = false;
  // Where the shape was read through when it was suspended, until it is opened there.
  #position: ShapePosition | null = null;
  // Bodies no reader has asked for, to read while no read a reader waits on is under way.
  readonly #ahead = new Map<string, BodyId>();
  #reading = 0;
  #draining = false;

  constructor(url: string, onGone: () => void, onAttempt: (failure: string | null) => void) {
    super();
    this.#url = url;
    this.#onGone = onGone;
    this.#onAttempt = onAttempt;
  }

  getVersion = (): number => this.#version;

  get error(): string | null {
    return this.#error;
  }

  want(reference: PayloadRef): void {
    const key = bodyKey(reference.owner_id, reference.generation);
    if (this.#requested.has(key)) return;
    this.#ahead.delete(key);
    const body = { ownerId: reference.owner_id, generation: reference.generation };
    this.#requested.set(key, body);
    this.#queue([body]);
  }

  /** Reads the body once nothing a reader waits on is being read, unless a reader asks for it first. */
  readAhead(reference: PayloadRef): void {
    const key = bodyKey(reference.owner_id, reference.generation);
    if (Number(reference.chunk_count) === 0 || this.#requested.has(key) || this.#ahead.has(key)) return;
    this.#ahead.set(key, { ownerId: reference.owner_id, generation: reference.generation });
    // Bodies of rows that arrive together share a read.
    if (!this.#draining) {
      this.#draining = true;
      queueMicrotask(() => {
        this.#draining = false;
        this.#drain();
      });
    }
  }

  /** The body as far as the reference spans it. Every prefix of a body's chunks is one of its
   * revisions, so until a chunk the reference names arrives, the body shows the one before. */
  body(reference: PayloadRef): string | null {
    const chunks = this.#chunks.get(bodyKey(reference.owner_id, reference.generation));
    const count = Number(reference.chunk_count);
    if (count === 0) return "";
    const parts: string[] = [];
    for (let index = 0; index < count; index++) {
      const text = chunks?.get(index);
      if (text === undefined) break;
      parts.push(text);
    }
    return parts.length === 0 ? null : parts.join("");
  }

  retry = (): void => {
    this.#shape?.close();
    this.#shape = null;
    this.#error = null;
    this.#changed();
    this.#queue([...this.#requested.values()]);
  };

  /** Stops following the log, keeping the chunks held and where in the log they are read through. */
  suspend(): void {
    this.#suspended = true;
    this.#position = this.#shape?.position ?? null;
    this.#shape?.close();
    this.#shape = null;
  }

  /** Follows the log on from where it was suspended. Where nothing says how far that was, the bodies
   * held are read again. */
  resume(): void {
    this.#suspended = false;
    if (this.#position === null) this.#queued = [...this.#requested.values()];
    else {
      this.#shape = this.#open(this.#position);
      this.#position = null;
    }
    if (this.#queued.length > 0) this.#flush();
    this.#drain();
  }

  close(): void {
    this.#closed = true;
    this.#shape?.close();
  }

  #open(from: ShapePosition | null): Shape {
    return new Shape(
      this.#url,
      (messages) => this.#apply(messages),
      (error) => this.#fail(error),
      this.#onAttempt,
      from
    );
  }

  #queue(references: BodyId[]): void {
    // Bodies mounted in one render share a read.
    if (this.#queued.length === 0) queueMicrotask(() => this.#flush());
    this.#queued.push(...references);
  }

  #flush(): void {
    if (this.#closed || this.#suspended) return;
    for (const batch of batches(this.#queued.splice(0), SUBSET_BODIES)) this.#read(batch);
  }

  #read(bodies: BodyId[]): void {
    const shape = (this.#shape ??= this.#open(null));
    this.#reading++;
    shape
      .subset(bodySubset(bodies))
      .catch((error: unknown) => {
        if (!shape.closed) this.#fail(error);
      })
      .finally(() => {
        this.#reading--;
        this.#drain();
      });
  }

  /** The next batch of bodies read ahead, once no other read is queued or under way. */
  #drain(): void {
    if (this.#closed || this.#suspended || this.#reading > 0 || this.#queued.length > 0) return;
    const batch = [...this.#ahead].slice(0, AHEAD_BODIES);
    for (const [key, body] of batch) {
      this.#ahead.delete(key);
      this.#requested.set(key, body);
    }
    if (batch.length > 0) this.#read(batch.map(([, body]) => body));
  }

  #apply(messages: Message<Row>[]): void {
    let changed = false;
    let retired = false;
    for (const message of messages) {
      if (!isChangeMessage(message)) {
        retired ||= message.headers.control === "must-refetch";
        continue;
      }
      // Chunks are insert-only. The field's log carries every body of the thread, and a reader
      // keeps the ones it shows.
      if (message.headers.operation !== "insert") continue;
      const chunk = chunkSchema.parse(message.value);
      const key = bodyKey(chunk.ownerId, chunk.generation.toString());
      if (!this.#requested.has(key)) continue;
      let chunks = this.#chunks.get(key);
      if (chunks === undefined) this.#chunks.set(key, (chunks = new Map()));
      chunks.set(Number(chunk.chunkIndex), chunk.text);
      changed = true;
    }
    if (changed) this.#changed();
    // The chunks written while the log was being rebuilt are not on it: read the bodies shown again.
    if (retired) this.#queue([...this.#requested.values()]);
  }

  #fail(error: unknown): void {
    if (error instanceof FetchError && error.status === 410) this.#onGone();
    else {
      this.#error = displayableError(error);
      this.#changed();
    }
  }

  #changed(): void {
    this.#version++;
    this.notify();
  }
}

interface WindowState {
  /** Every row the reader holds. */
  rows: readonly ThreadEntity[];
  /** The tail, view state and pending commands have loaded, and reach the scope's cursor. */
  caughtUp: boolean;
  olderAvailable: boolean;
  loadingOlder: boolean;
  connection: StreamConnection;
  error: string | null;
}

const NO_WINDOW: WindowState = {
  rows: [],
  caughtUp: false,
  olderAvailable: false,
  loadingOlder: false,
  connection: { phase: "live", since: 0 },
  error: null,
};

/** A thread's rows at one projection epoch: the pages a reader has loaded, and what it waits on. */
class EpochWindow extends Listeners {
  readonly epoch: string;
  readonly #threadId: string;
  #shape: Shape;
  readonly #onGone: () => void;
  // Keyed by Electric's row key, which a delete carries without the row.
  readonly #rows = new Map<string, ThreadEntity>();
  readonly #commands = new Set<string>();
  readonly #bodies = new Map<PayloadField, PayloadShape>();
  // Pages load one at a time, each before the oldest row the last one held.
  #pages: Promise<void> = Promise.resolve();
  #lowest: bigint | null = null;
  #exhausted = false;
  #loadingOlder = false;
  // The epoch is gone: the window that replaces this one loads what it would have.
  #gone = false;
  #ready = false;
  // Rows seen since a refetch began; what it did not see was deleted while the log was rebuilt.
  #refreshed: Set<string> | null = null;
  // The shapes, by path, whose client is retrying a failed request.
  readonly #retrying = new Set<string>();
  #connection: StreamConnection = { phase: "live", since: Date.now() };
  #closed = false;
  #suspended = false;
  // The last event the fold had applied when the scope was read; null while suspended, until it is
  // read again, as the rows held reach no scope then.
  #through: bigint | null;
  // Where the entities' log was read through when the window was suspended.
  #position: ShapePosition | null = null;
  #state: WindowState = NO_WINDOW;

  constructor(threadId: string, scope: ThreadScope, onGone: () => void) {
    super();
    this.#threadId = threadId;
    this.epoch = scope.projection_epoch;
    this.#through = BigInt(scope.through_cursor);
    this.#onGone = onGone;
    this.#shape = this.#open(null);
    void this.#guard(async () => {
      await Promise.all([
        this.#serial(() => this.#catchUp(INITIAL_ROWS)),
        this.#shape.subset(VIEW_STATE),
        this.#shape.subset(PENDING_COMMANDS),
      ]);
      this.#ready = true;
      this.#publish();
    });
  }

  getState = (): WindowState => this.#state;

  loadOlder = (): void => {
    // A stopped or retired window loads nothing more, or a view asking whenever the reader is at the
    // top would repeat a failed page for as long as they stay there.
    if (this.#loadingOlder || this.#exhausted || this.#lowest === null || this.#gone || this.#state.error !== null)
      return;
    this.#loadingOlder = true;
    this.#publish();
    void this.#guard(() => this.#serial(() => this.#page(PAGE))).finally(() => {
      this.#loadingOlder = false;
      this.#publish();
    });
  };

  selectCommands(ids: readonly string[]): void {
    const unseen = [...new Set(ids)].filter((id) => !this.#commands.has(id));
    for (const id of unseen) this.#commands.add(id);
    for (const batch of batches(unseen, SUBSET_ROWS)) void this.#guard(() => this.#shape.subset(commandsById(batch)));
  }

  bodies(field: PayloadField): PayloadShape {
    let shape = this.#bodies.get(field);
    if (shape === undefined)
      this.#bodies.set(
        field,
        (shape = new PayloadShape(this.#url(`chunks/${field}`), this.#onGone, (failure) =>
          this.#attempted(field, failure)
        ))
      );
    return shape;
  }

  get suspended(): boolean {
    return this.#suspended;
  }

  /** Whether a suspended window can follow its thread on: it had loaded what it was asked to, and
   * nothing has stopped it or retired its epoch since. */
  get resumable(): boolean {
    return this.#ready && this.#refreshed === null && !this.#gone && this.#state.error === null;
  }

  /** Stops following the thread, keeping the rows and bodies held and where in each log they are read through. */
  suspend(): void {
    this.#suspended = true;
    this.#through = null;
    this.#position = this.#shape.position;
    this.#shape.close();
    for (const shape of this.#bodies.values()) shape.suspend();
    this.#publish();
  }

  /** Follows the thread on from where it was suspended, to `through` and beyond. Where nothing says
   * how far its log was read, the rows held are read again. */
  resume(through: bigint): void {
    this.#suspended = false;
    this.#through = through;
    this.#connection = { phase: "live", since: Date.now() };
    this.#retrying.clear();
    this.#shape = this.#open(this.#position);
    for (const shape of this.#bodies.values()) shape.resume();
    if (this.#position === null) void this.#refetch();
    this.#position = null;
    this.#publish();
  }

  close(): void {
    this.#closed = true;
    this.#shape.close();
    for (const shape of this.#bodies.values()) shape.close();
  }

  #open(from: ShapePosition | null): Shape {
    return new Shape(
      this.#url("entities"),
      (messages) => this.#apply(messages),
      (error) => this.#fail(error),
      (failure) => this.#attempted("entities", failure),
      from
    );
  }

  #url(path: string): string {
    const url = new URL(`/threads/${encodeURIComponent(this.#threadId)}/sync/${path}`, window.location.href);
    url.searchParams.set("projection_epoch", this.epoch);
    return url.toString();
  }

  #serial<T>(load: () => Promise<T>): Promise<T> {
    const next = this.#pages.then(load);
    // A failed page reaches its caller; the next one still loads after it.
    this.#pages = next.then(
      () => undefined,
      () => undefined
    );
    return next;
  }

  /** The page before the oldest row held, or the tail; how many rows it read. */
  async #page(limit: number): Promise<number> {
    const rows = await this.#shape.subset(
      this.#lowest === null
        ? { orderBy: NEWEST_FIRST, limit }
        : { where: "entity_index < $1", params: { "1": this.#lowest.toString() }, orderBy: NEWEST_FIRST, limit }
    );
    for (const row of rows) {
      const index = decimalBigInt(entitySchema.parse(row).entityIndex);
      if (this.#lowest === null || index < this.#lowest) this.#lowest = index;
    }
    this.#exhausted = rows.length < limit || this.#lowest === 0n;
    return rows.length;
  }

  /** Enough PAGE-sized reads, oldest-ward from wherever #lowest already is, to hold at least
   * `rows` -- or the whole thread, if it is shorter than that. */
  async #catchUp(rows: number): Promise<void> {
    let loaded = 0;
    while (loaded < rows && !this.#exhausted) loaded += await this.#page(PAGE);
  }

  /** After Electric retires the shape's log, as many rows again, kept on screen meanwhile. */
  async #refetch(): Promise<void> {
    const held = this.#lowest;
    let remaining =
      held === null
        ? INITIAL_ROWS
        : [...this.#rows.values()].filter((row) => decimalBigInt(row.entityIndex) >= held).length;
    this.#refreshed = new Set();
    await this.#guard(async () => {
      await Promise.all([
        this.#serial(async () => {
          this.#lowest = null;
          this.#exhausted = false;
          while (remaining > 0 && !this.#exhausted) remaining -= await this.#page(Math.min(SUBSET_ROWS, remaining));
        }),
        this.#shape.subset(VIEW_STATE),
        this.#shape.subset(PENDING_COMMANDS),
        ...batches([...this.#commands], SUBSET_ROWS).map((batch) => this.#shape.subset(commandsById(batch))),
      ]);
      const refreshed = this.#refreshed ?? new Set<string>();
      for (const key of this.#rows.keys()) if (!refreshed.has(key)) this.#rows.delete(key);
    });
    this.#refreshed = null;
    this.#publish();
  }

  #apply(messages: Message<Row>[]): void {
    let changed = false;
    let retired = false;
    for (const message of messages) {
      if (isChangeMessage(message)) {
        this.#refreshed?.add(message.key);
        if (message.headers.operation === "delete") {
          // The fold never deletes a row: its view state leaves the shape when the epoch is retired.
          // An SSE connection stays open, so this is sooner than the 410 its reconnect would get.
          retired ||= this.#rows.get(message.key)?.entityKind === "view_state";
          changed = this.#rows.delete(message.key) || changed;
        }
        // Every row is new at the tail. A change to one older than the reader has loaded is not
        // its concern: loading that page reads the row as it is by then.
        else if (message.headers.operation === "insert" || this.#rows.has(message.key)) {
          this.#rows.set(message.key, entitySchema.parse(message.value));
          changed = true;
        }
      } else if (message.headers.control === "must-refetch") void this.#refetch();
    }
    if (changed) this.#publish();
    if (retired && !this.#closed) this.#retire();
  }

  async #guard(load: () => Promise<unknown>): Promise<void> {
    try {
      await load();
    } catch (error) {
      this.#fail(error);
    }
  }

  #fail(error: unknown): void {
    if (this.#closed) return;
    // The fold was rebuilt under a new epoch: the reader resolves the thread's scope again.
    if (error instanceof FetchError && error.status === 410) this.#retire();
    else this.#publish(displayableError(error));
  }

  #retire(): void {
    this.#gone = true;
    this.#onGone();
  }

  /** One shape's attempt: a failure counts against the window until every shape it has has recovered. */
  #attempted(shape: string, failure: string | null): void {
    const was = this.#connection;
    if (failure !== null) {
      this.#retrying.add(shape);
      this.#connection =
        was.phase === "reconnecting"
          ? { ...was, attempt: was.attempt + 1, lastError: failure }
          : { phase: "reconnecting", since: Date.now(), attempt: 1, lastError: failure };
    } else if (this.#retrying.delete(shape) && this.#retrying.size === 0) {
      this.#connection = { phase: "live", since: Date.now() };
    }
    this.#publish();
  }

  #publish(error: string | null = this.#state.error): void {
    const rows = [...this.#rows.values()];
    if (!this.#suspended)
      for (const row of rows)
        for (const reference of [row.textRef, row.argumentsRef, row.outputRef, row.inputRef])
          if (reference !== null) this.bodies(reference.field).readAhead(reference);
    const view = rows.find((row) => row.entityKind === "view_state");
    this.#state = {
      rows,
      caughtUp:
        this.#ready &&
        this.#through !== null &&
        view !== undefined &&
        decimalBigInt(view.revisionCursor) >= this.#through,
      olderAvailable: this.#lowest !== null && !this.#exhausted,
      loadingOlder: this.#loadingOlder,
      connection: this.#connection,
      error,
    };
    this.notify();
  }
}

interface SyncState {
  /** The window on screen. A replacement stays off screen until it has caught up. */
  window: EpochWindow | null;
  error: string | null;
}

/** A thread's scope, and the window over it: replaced whole when its epoch is gone. */
class ThreadEpochs extends Listeners {
  readonly #threadId: string;
  #next: EpochWindow | null = null;
  #resolving: AbortController | null = null;
  #retry: number | undefined;
  #closed = false;
  #suspended = false;
  #state: SyncState = { window: null, error: null };

  constructor(threadId: string) {
    super();
    this.#threadId = threadId;
    void this.#resolve();
  }

  getState = (): SyncState => this.#state;

  refresh = (): void => {
    // A suspended thread follows nothing, whatever a read it had in flight finds.
    if (!this.#suspended) void this.#resolve();
  };

  /** Stops following the thread; its window stays, off the network, until `resume` or `close`. */
  suspend(): void {
    this.#suspended = true;
    this.#resolving?.abort();
    window.clearTimeout(this.#retry);
    this.#next?.close();
    this.#next = null;
    this.#state.window?.suspend();
  }

  resume(): void {
    this.#suspended = false;
    void this.#resolve();
  }

  close(): void {
    this.#closed = true;
    this.#resolving?.abort();
    window.clearTimeout(this.#retry);
    this.#next?.close();
    this.#state.window?.close();
  }

  async #resolve(): Promise<void> {
    window.clearTimeout(this.#retry);
    this.#resolving?.abort();
    const resolving = (this.#resolving = new AbortController());
    try {
      const scope = await threadScope(this.#threadId, resolving.signal);
      if (resolving.signal.aborted || this.#closed) return;
      // The runner has recorded nothing for the whole of the server's hold: hold another read.
      if (scope === null) return this.refresh();
      const suspended = this.#state.window?.suspended ? this.#state.window : null;
      if (suspended?.resumable && suspended.epoch === scope.projection_epoch) {
        suspended.resume(BigInt(scope.through_cursor));
        return this.#set({ window: suspended, error: null });
      }
      this.#next?.close();
      const next = new EpochWindow(this.#threadId, scope, this.refresh);
      // The first window shows its own catch-up; a replacement takes over once it has caught up,
      // or has an error to show.
      if (this.#state.window === null) return this.#set({ window: next, error: null });
      this.#next = next;
      const unsubscribe = next.subscribe(() => {
        const state = next.getState();
        if (this.#next !== next || (!state.caughtUp && state.error === null)) return;
        unsubscribe();
        this.#next = null;
        this.#state.window?.close();
        this.#set({ window: next, error: null });
      });
      this.#set({ ...this.#state, error: null });
    } catch (error) {
      if (resolving.signal.aborted || this.#closed) return;
      this.#set({ ...this.#state, error: displayableError(error) });
      // A failed read reconnects after a pause; an answered one never waits.
      this.#retry = window.setTimeout(this.refresh, 1_000);
    }
  }

  #set(state: SyncState): void {
    this.#state = state;
    this.notify();
  }
}

const NO_SYNC: SyncState = { window: null, error: null };
const noSubscription = (): (() => void) => () => undefined;

const ThreadContext = createContext<ThreadEpochs | null>(null);

function useEpochs(): SyncState {
  const thread = useContext(ThreadContext);
  return useSyncExternalStore(thread?.subscribe ?? noSubscription, thread?.getState ?? (() => NO_SYNC));
}

function useWindow(): EpochWindow {
  const { window: shown } = useEpochs();
  if (shown === null) throw new Error("thread rows are read inside a Thread, once its window is open");
  return shown;
}

/** Keep settled input outcomes in the session projection; other settled commands are selected by id. */
function threadRow(row: ThreadEntity): boolean {
  return (
    row.entityKind !== "command" ||
    row.pending ||
    ("outcome" in row.state && row.state.operation === "submit_input" && row.state.outcome !== "pending")
  );
}

// How many threads a reader can leave and come back to without their windows being read again.
const RETAINED_THREADS = 8;

/** The threads a reader has left, least recently left first, each suspended. */
class RetainedThreads {
  readonly #left = new Map<string, ThreadEpochs>();

  /** The thread's retained window, resumed, or else a new one. */
  open(threadId: string): ThreadEpochs {
    const retained = this.#left.get(threadId);
    if (retained === undefined) return new ThreadEpochs(threadId);
    this.#left.delete(threadId);
    retained.resume();
    return retained;
  }

  leave(threadId: string, epochs: ThreadEpochs): void {
    epochs.suspend();
    this.#left.get(threadId)?.close();
    this.#left.delete(threadId);
    this.#left.set(threadId, epochs);
    for (const [evicted, forgotten] of this.#left) {
      if (this.#left.size <= RETAINED_THREADS) break;
      forgotten.close();
      this.#left.delete(evicted);
    }
  }
}

/** A thread sync with a cache of left threads' windows of its own, so instances (a test's, say) share none. */
export function createElectricThreadSync(): ThreadSync {
  const retained = new RetainedThreads();

  function Thread({ threadId, children }: { threadId: string; children: ReactNode }): JSX.Element {
    const [thread, setThread] = useState<ThreadEpochs | null>(null);
    useEffect(() => {
      const next = retained.open(threadId);
      setThread(next);
      return () => retained.leave(threadId, next);
    }, [threadId]);
    return <ThreadContext.Provider value={thread}>{children}</ThreadContext.Provider>;
  }

  return { Thread, useThread, useCommandRows, usePayload };
}

function useThread(): ThreadState {
  const thread = useContext(ThreadContext);
  const { window: shown, error } = useEpochs();
  const state = useSyncExternalStore(shown?.subscribe ?? noSubscription, shown?.getState ?? (() => NO_WINDOW));
  return {
    window:
      thread === null || shown === null
        ? null
        : {
            rows: state.rows.filter(threadRow),
            caughtUp: state.caughtUp,
            olderAvailable: state.olderAvailable,
            loadingOlder: state.loadingOlder,
            loadOlder: shown.loadOlder,
            connection: state.connection,
            error: state.error,
            refresh: thread.refresh,
          },
    error,
  };
}

function useCommandRows(commandIds: readonly string[]): ThreadEntity[] {
  const shown = useWindow();
  const { rows } = useSyncExternalStore(shown.subscribe, shown.getState);
  const key = [...new Set(commandIds)].sort().join("\u0000");
  useEffect(() => {
    if (key) shown.selectCommands(key.split("\u0000"));
  }, [key, shown]);
  const selected = new Set(commandIds);
  return rows.filter((row) => row.entityKind === "command" && selected.has(row.entityId));
}

function usePayload(reference: PayloadRef): Payload {
  const shape = useWindow().bodies(reference.field);
  useSyncExternalStore(shape.subscribe, shape.getVersion);
  useEffect(() => {
    shape.want(reference);
  }, [reference, shape]);
  return { body: shape.body(reference), error: shape.error, retry: shape.retry };
}

export const electricThreadSync: ThreadSync = createElectricThreadSync();
