/**
 * A thread's rows and bodies, synchronized from Electric.
 *
 * A thread is one shape over its rows, and each payload field one shape over its bodies' chunks,
 * both opened from now with `log=changes_only`. The rows a reader holds — the tail, each page it
 * scrolls back to, the view state, the commands it is waiting on, the bodies in view — arrive as
 * subsets of those shapes, and every later change to them on the shapes' live logs. A window
 * therefore moves by loading more, never by opening another shape.
 */
import {
  FetchError,
  ShapeStream,
  isChangeMessage,
  snakeToCamel,
  type ColumnMapper,
  type Message,
  type Row,
  type SubsetParams,
} from "@electric-sql/client";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState, useSyncExternalStore } from "react";
import { z } from "zod";

import { displayableError, threadScope, type ThreadScope } from "./client";
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

// Rows per page: the tail a reader opens on, and each page before the oldest row it holds.
const PAGE = 30;
// The proxy's bounds on one subset read.
const SUBSET_ROWS = 200;
const SUBSET_BODIES = 100;
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

function bodyKey(ownerId: string, generation: string): string {
  return `${ownerId}\u0000${generation}`;
}

function bodySubset(references: readonly { ownerId: string; generation: string }[]): SubsetParams {
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
// CLEANUP(added 2026-09-23): Drop once a released @electric-sql/client moves only a stream at `now`
//   to a subset's offset; 1.5.28's requestSnapshot moves a live one too (LiveState.handleResponseMetadata).
async function keepingOffset(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init);
  const offset = new URL(input instanceof Request ? input.url : String(input)).searchParams.get("offset");
  if (init?.method !== "POST" || !response.ok || offset === null || offset === "now") return response;
  const headers = new Headers(response.headers);
  headers.set("electric-offset", offset);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

/**
 * One Electric shape from now: its rows arrive as subsets of it, and as its live changes, followed
 * over SSE. Electric's client long-polls a shape instead once three SSE responses in a row have
 * ended within a second, as Electric's answers to a reader behind the log do.
 */
class Shape {
  readonly #abort = new AbortController();
  readonly #stream: ShapeStream<Row>;
  // Settles with the shape's first subset. Electric answers that one from the shape's definition
  // with the handle and offset the stream then follows; later subsets name that handle.
  #opened: Promise<unknown> | null = null;

  constructor(url: string, onMessages: (messages: Message<Row>[]) => void, onError: (error: unknown) => void) {
    this.#stream = new ShapeStream({
      url,
      offset: "now",
      log: "changes_only",
      liveSse: true,
      subsetMethod: "POST",
      columnMapper: columns,
      fetchClient: keepingOffset,
      signal: this.#abort.signal,
      onError: (error) => {
        if (!this.#abort.signal.aborted) onError(error);
      },
    });
    this.#stream.subscribe(onMessages);
  }

  /** Rows of the shape, delivered to its subscriber like any change and returned. */
  async subset(params: SubsetParams): Promise<Row[]> {
    const opened = this.#opened;
    const request =
      opened === null ? this.#stream.requestSnapshot(params) : opened.then(() => this.#stream.requestSnapshot(params));
    // A failed first subset reaches its own caller; the ones after it still go.
    this.#opened ??= request.catch(() => undefined);
    const { data } = await request;
    return data.map((message) => message.value);
  }

  get closed(): boolean {
    return this.#abort.signal.aborted;
  }

  close(): void {
    this.#abort.abort();
  }
}

/** The bodies of one payload field that a reader shows, from the field's shape. */
class PayloadShape extends Listeners {
  readonly #url: string;
  readonly #onGone: () => void;
  #shape: Shape | null = null;
  // Chunk text by index, per body: an owner at one generation.
  readonly #chunks = new Map<string, Map<number, string>>();
  readonly #requested = new Map<string, { ownerId: string; generation: string }>();
  #queued: { ownerId: string; generation: string }[] = [];
  #version = 0;
  #error: string | null = null;
  #closed = false;

  constructor(url: string, onGone: () => void) {
    super();
    this.#url = url;
    this.#onGone = onGone;
  }

  getVersion = (): number => this.#version;

  get error(): string | null {
    return this.#error;
  }

  want(reference: PayloadRef): void {
    const key = bodyKey(reference.owner_id, reference.generation);
    if (this.#requested.has(key)) return;
    const body = { ownerId: reference.owner_id, generation: reference.generation };
    this.#requested.set(key, body);
    this.#queue([body]);
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

  close(): void {
    this.#closed = true;
    this.#shape?.close();
  }

  #queue(references: { ownerId: string; generation: string }[]): void {
    // Bodies mounted in one render share a read.
    if (this.#queued.length === 0) queueMicrotask(() => this.#flush());
    this.#queued.push(...references);
  }

  #flush(): void {
    if (this.#closed) return;
    const shape = (this.#shape ??= new Shape(
      this.#url,
      (messages) => this.#apply(messages),
      (error) => this.#fail(error)
    ));
    for (const batch of batches(this.#queued.splice(0), SUBSET_BODIES))
      shape.subset(bodySubset(batch)).catch((error: unknown) => {
        if (!shape.closed) this.#fail(error);
      });
  }

  #apply(messages: Message<Row>[]): void {
    let changed = false;
    for (const message of messages) {
      // Chunks are insert-only. The field's log carries every body of the thread, and a reader
      // keeps the ones it shows.
      if (!isChangeMessage(message) || message.headers.operation !== "insert") continue;
      const chunk = chunkSchema.parse(message.value);
      const key = bodyKey(chunk.ownerId, chunk.generation.toString());
      if (!this.#requested.has(key)) continue;
      let chunks = this.#chunks.get(key);
      if (chunks === undefined) this.#chunks.set(key, (chunks = new Map()));
      chunks.set(Number(chunk.chunkIndex), chunk.text);
      changed = true;
    }
    if (changed) this.#changed();
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
  error: string | null;
}

/** A thread's rows at one projection epoch: the pages a reader has loaded, and what it waits on. */
class EpochWindow extends Listeners {
  readonly scope: ThreadScope;
  readonly #threadId: string;
  readonly #shape: Shape;
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
  #closed = false;
  #state: WindowState = { rows: [], caughtUp: false, olderAvailable: false, loadingOlder: false, error: null };

  constructor(threadId: string, scope: ThreadScope, onGone: () => void) {
    super();
    this.#threadId = threadId;
    this.scope = scope;
    this.#onGone = onGone;
    this.#shape = new Shape(
      this.#url("entities"),
      (messages) => this.#apply(messages),
      (error) => this.#fail(error)
    );
    void this.#guard(async () => {
      await Promise.all([
        this.#serial(() => this.#page(PAGE)),
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
      this.#bodies.set(field, (shape = new PayloadShape(this.#url(`chunks/${field}`), this.#onGone)));
    return shape;
  }

  close(): void {
    this.#closed = true;
    this.#shape.close();
    for (const shape of this.#bodies.values()) shape.close();
  }

  #url(path: string): string {
    const url = new URL(`/threads/${encodeURIComponent(this.#threadId)}/sync/${path}`, window.location.href);
    url.searchParams.set("projection_epoch", this.scope.projection_epoch);
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

  /** After Electric retires the shape's log, as many rows again, kept on screen meanwhile. */
  async #refetch(): Promise<void> {
    const held = this.#lowest;
    let remaining =
      held === null ? PAGE : [...this.#rows.values()].filter((row) => decimalBigInt(row.entityIndex) >= held).length;
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

  #publish(error: string | null = this.#state.error): void {
    const rows = [...this.#rows.values()];
    const view = rows.find((row) => row.entityKind === "view_state");
    this.#state = {
      rows,
      caughtUp:
        this.#ready && view !== undefined && decimalBigInt(view.revisionCursor) >= BigInt(this.scope.through_cursor),
      olderAvailable: this.#lowest !== null && !this.#exhausted,
      loadingOlder: this.#loadingOlder,
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
  #state: SyncState = { window: null, error: null };

  constructor(threadId: string) {
    super();
    this.#threadId = threadId;
    void this.#resolve();
  }

  getState = (): SyncState => this.#state;

  refresh = (): void => {
    void this.#resolve();
  };

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
const NO_WINDOW: WindowState = { rows: [], caughtUp: false, olderAvailable: false, loadingOlder: false, error: null };
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

/** Settled commands are the command list's to show, by id, not the thread's. */
function threadRow(row: ThreadEntity): boolean {
  return row.entityKind !== "command" || row.pending;
}

function Thread({ threadId, children }: { threadId: string; children: ReactNode }): JSX.Element {
  const [thread, setThread] = useState<ThreadEpochs | null>(null);
  useEffect(() => {
    const next = new ThreadEpochs(threadId);
    setThread(next);
    return () => next.close();
  }, [threadId]);
  return <ThreadContext.Provider value={thread}>{children}</ThreadContext.Provider>;
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

export const electricThreadSync: ThreadSync = { Thread, useThread, useCommandRows, usePayload };
