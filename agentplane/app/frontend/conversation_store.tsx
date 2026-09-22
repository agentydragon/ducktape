import { FetchError, snakeCamelMapper } from "@electric-sql/client";
import { electricCollectionOptions } from "@tanstack/electric-db-collection";
import { createCollection, useLiveQuery } from "@tanstack/react-db";
import { createContext, type JSX, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";

import { conversationInterest, displayableError, type ConversationStoredEntity, type EntityInterest } from "./client";

const decimal: z.ZodType<string | bigint> = z.union([z.string().regex(/^-?\d+$/), z.bigint()]);
const RefreshConversation = createContext<() => void>(() => undefined);
declare global {
  interface Window {
    __agentplaneConversationCollectionTrace?: unknown[];
  }
}
type Decimal = z.output<typeof decimal>;
export function decimalBigInt(value: Decimal): bigint {
  return typeof value === "bigint" ? value : BigInt(value);
}
export type PayloadRef = NonNullable<ConversationStoredEntity["text_ref"]>;
type ConversationState = ConversationStoredEntity["state"];

export interface ConversationEntity {
  threadId: string;
  sourceId: string;
  projectionEpoch: string;
  entityKind: "view_state" | "item" | "confirmed_input" | "lifecycle" | "command";
  entityId: string;
  cursor: Decimal;
  revisionCursor: Decimal;
  pending: boolean;
  turnId: string | null;
  state: ConversationState;
  textRef: PayloadRef | null;
  argumentsRef: PayloadRef | null;
  outputRef: PayloadRef | null;
  inputRef: PayloadRef | null;
}
const payloadRefSchema = z.object({
  source_id: z.string(),
  projection_epoch: z.string(),
  owner_cursor: z.string(),
  owner_item_id: z.string(),
  field: z.enum(["text", "arguments", "output", "confirmed_input", "command_input"]),
  revision_cursor: z.string(),
  generation: z.string(),
  content_bytes: z.string(),
});

const stateSchema = z.union([
  z.object({
    controls: z.object({
      applied_model: z.string().nullable(),
      active_turn_id: z.string().nullable(),
      harness_state: z.string().nullable(),
    }),
    unresolved_count: z.number().int(),
    operational: z.object({
      operational_version: z.string(),
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
  sourceId: z.string(),
  projectionEpoch: z.string(),
  entityKind: z.enum(["view_state", "item", "confirmed_input", "lifecycle", "command"]),
  entityId: z.string(),
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
  threadId: z.string(),
  sourceId: z.string(),
  projectionEpoch: z.string(),
  ownerCursor: decimal,
  ownerId: z.string(),
  field: z.string(),
  generation: decimal,
  chunkIndex: decimal,
  text: z.string(),
});
type PayloadChunk = z.output<typeof chunkSchema>;

function contentUrl(threadId: string, interest: EntityInterest): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/content`, window.location.href);
  url.searchParams.set("source_id", interest.source_id);
  url.searchParams.set("projection_epoch", interest.projection_epoch);
  url.searchParams.set("anchor_cursor", interest.anchor_cursor);
  url.searchParams.set("tail_from", interest.tail_from);
  if (interest.window_from !== null) url.searchParams.set("window_from", interest.window_from);
  if (interest.window_before !== null) url.searchParams.set("window_before", interest.window_before);
  return url.toString();
}

/**
 * Every body the window renders, in one shape alongside the entities that name them.
 *
 * A shape is a partition, not a viewport: this one is keyed by the same bounds as the entity
 * collection, so opening the same tail twice asks the server for a shape it already holds, and an
 * item arriving during a turn is already inside the predicate rather than a new shape of its own.
 */
function contentCollection(threadId: string, interest: EntityInterest, onError: (error: unknown) => void) {
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-content:${threadId}:${interest.source_id}:${interest.projection_epoch}:${interest.tail_from}:${interest.window_from ?? "tail"}`,
      gcTime: 1_000,
      schema: chunkSchema,
      getKey: (row) => `${row.ownerId}:${row.field}:${row.generation}:${row.chunkIndex}`,
      syncMode: "eager",
      shapeOptions: { url: contentUrl(threadId, interest), columnMapper: snakeCamelMapper(), subscribe: true, onError },
    })
  );
}

type ContentCollection = ReturnType<typeof contentCollection>;
// Null outside a conversation window: a body of an entity no window covers reads on its own.
const WindowContent = createContext<ContentCollection | null>(null);

function entityUrl(threadId: string, interest: EntityInterest): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/entities`, window.location.href);
  url.searchParams.set("source_id", interest.source_id);
  url.searchParams.set("projection_epoch", interest.projection_epoch);
  url.searchParams.set("anchor_cursor", interest.anchor_cursor);
  url.searchParams.set("tail_from", interest.tail_from);
  if (interest.window_from !== null) url.searchParams.set("window_from", interest.window_from);
  if (interest.window_before !== null) url.searchParams.set("window_before", interest.window_before);
  return url.toString();
}

function entityCollection(threadId: string, interest: EntityInterest, onError: (error: unknown) => void) {
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-conversation:${threadId}:${interest.source_id}:${interest.projection_epoch}:${interest.anchor_cursor}:${interest.window_from ?? "tail"}`,
      gcTime: 1_000,
      schema: entitySchema,
      getKey: (row) => `${row.entityKind}:${row.entityId}`,
      syncMode: "on-demand",
      shapeOptions: {
        url: entityUrl(threadId, interest),
        params: { log: "changes_only" },
        columnMapper: snakeCamelMapper(),
        onError,
      },
    })
  );
}

function traceEntityCollection(
  kind: string,
  role: "active" | "pending",
  collection: ReturnType<typeof entityCollection>
): boolean {
  const trace = window.__agentplaneConversationCollectionTrace;
  if (!trace) return false;
  trace.push({
    kind,
    role,
    id: collection.id,
    size: collection.size,
    subscriberCount: collection.subscriberCount,
    status: collection.status,
    ready: collection.isReady(),
  });
  if (trace.length > 256) trace.splice(0, trace.length - 256);
  return true;
}

function commandUrl(
  threadId: string,
  sourceId: string,
  projectionEpoch: string,
  commandIds: readonly string[]
): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/commands`, window.location.href);
  url.searchParams.set("source_id", sourceId);
  url.searchParams.set("projection_epoch", projectionEpoch);
  for (const id of [...new Set(commandIds)].sort()) url.searchParams.append("command_id", id);
  return url.toString();
}

function commandCollection(
  threadId: string,
  sourceId: string,
  projectionEpoch: string,
  commandIds: readonly string[],
  onError: (error: unknown) => void
) {
  const selected = [...new Set(commandIds)].sort();
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-commands:${threadId}:${sourceId}:${projectionEpoch}:${selected.join(":")}`,
      gcTime: 1_000,
      schema: entitySchema,
      getKey: (row) => row.entityId,
      syncMode: "on-demand",
      shapeOptions: {
        url: commandUrl(threadId, sourceId, projectionEpoch, selected),
        params: { log: "changes_only" },
        columnMapper: snakeCamelMapper(),
        onError,
      },
    })
  );
}

export function CommandSelection({
  threadId,
  sourceId,
  projectionEpoch,
  commandIds,
  children,
}: {
  threadId: string;
  sourceId: string;
  projectionEpoch: string;
  commandIds: readonly string[];
  children: (rows: ConversationEntity[]) => JSX.Element;
}): JSX.Element {
  const key = [...new Set(commandIds)].sort().join("\u0000");
  const [attempt, setAttempt] = useState(0);
  const [streamError, setStreamError] = useState<{
    collection: ReturnType<typeof commandCollection>;
    message: string;
  } | null>(null);
  const currentCollection = useRef<ReturnType<typeof commandCollection> | null>(null);
  const retry = useCallback(() => {
    setStreamError(null);
    setAttempt((value) => value + 1);
  }, []);
  const collection = useMemo(() => {
    let next: ReturnType<typeof commandCollection>;
    next = commandCollection(threadId, sourceId, projectionEpoch, key.split("\u0000"), (reason) => {
      if (currentCollection.current !== next) return;
      setStreamError({ collection: next, message: displayableError(reason) });
    });
    return next;
  }, [attempt, key, projectionEpoch, sourceId, threadId]);
  useEffect(() => {
    currentCollection.current = collection;
    return () => {
      if (currentCollection.current === collection) currentCollection.current = null;
    };
  }, [collection]);
  useEffect(() => {
    setStreamError((previous) => (previous?.collection === collection ? previous : null));
  }, [collection]);
  const query = useLiveQuery((q) => q.from({ command: collection }), [collection]);
  const stopped =
    streamError?.collection === collection
      ? streamError.message
      : query.isError
        ? "The command query entered an error state."
        : null;
  return (
    <>
      {stopped && (
        <p role="alert">
          Command synchronization stopped: {stopped} <button onClick={retry}>Retry command synchronization</button>
        </p>
      )}
      {children(query.data ?? [])}
    </>
  );
}

function ActiveConversation({
  threadId,
  interest,
  collection,
  content,
  onRows,
  onRotate,
  onCaughtUp,
  role,
}: {
  threadId: string;
  interest: EntityInterest;
  collection: ReturnType<typeof entityCollection>;
  content: ContentCollection;
  onRows: (rows: ConversationEntity[], interest: EntityInterest) => JSX.Element;
  onRotate: () => void;
  onCaughtUp?: () => void;
  role: "active" | "pending";
}): JSX.Element {
  const query = useLiveQuery((q) => q.from({ entity: collection }), [collection]);
  const rows = query.data ?? [];
  const view = rows.find((row) => row.entityKind === "view_state");
  const caughtUp = view !== undefined && decimalBigInt(view.revisionCursor) >= BigInt(interest.through_cursor);
  const segmentCount = rows.filter((row) => ["item", "confirmed_input", "lifecycle"].includes(row.entityKind)).length;
  useEffect(() => {
    traceEntityCollection("subscribed", role, collection);
    return () => {
      if (traceEntityCollection("unsubscribed", role, collection))
        collection.once("status:cleaned-up", () => traceEntityCollection("collected", role, collection));
    };
  }, [collection, role]);
  useEffect(() => {
    traceEntityCollection("query", role, collection);
  }, [collection, query.isError, rows, role]);
  useEffect(() => {
    if (segmentCount > 60) onRotate();
  }, [onRotate, segmentCount]);
  useEffect(() => {
    if (caughtUp) onCaughtUp?.();
  }, [caughtUp, onCaughtUp]);
  useEffect(() => {
    if (!query.isError) return;
    const retry = window.setTimeout(onRotate, 1_000);
    return () => window.clearTimeout(retry);
  }, [onRotate, query.isError]);
  return (
    <RefreshConversation.Provider value={onRotate}>
      <WindowContent.Provider value={content}>
        {query.isError && <p role="alert">Conversation synchronization stopped.</p>}
        {!query.isError && !caughtUp && (
          <p role="status" data-conversation-catchup="true">
            Catching up conversation…
          </p>
        )}
        {onRows(caughtUp ? rows : [], interest)}
      </WindowContent.Provider>
    </RefreshConversation.Provider>
  );
}

export function ConversationCollection({
  threadId,
  beforeCursor,
  children,
}: {
  threadId: string;
  beforeCursor?: string;
  children: (rows: ConversationEntity[], interest: EntityInterest) => JSX.Element;
}): JSX.Element {
  type Selection = {
    interest: EntityInterest;
    collection: ReturnType<typeof entityCollection>;
    content: ContentCollection;
  };
  const [selection, setSelection] = useState<Selection | null>(null);
  const selectionRef = useRef<Selection | null>(null);
  const [pendingSelection, setPendingSelection] = useState<Selection | null>(null);
  const pendingSelectionRef = useRef<Selection | null>(null);
  const [interestError, setInterestError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const recoveringSelectionRef = useRef<Selection | null>(null);
  const rotate = useCallback(() => {
    setStreamError(null);
    setGeneration((value) => value + 1);
  }, []);
  useEffect(
    () => () => {
      selectionRef.current = null;
      pendingSelectionRef.current = null;
      recoveringSelectionRef.current = null;
    },
    []
  );
  useEffect(() => {
    const controller = new AbortController();
    let retry: number | undefined;
    setInterestError(null);
    void conversationInterest(threadId, beforeCursor, controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) {
          let next: Selection;
          const onStreamError = (reason: unknown): void => {
            if (selectionRef.current !== next && pendingSelectionRef.current !== next) return;
            // The adapter preserves a ready collection after terminal stream errors.
            // A disconnected stream (status 0) and an expired app interest (410) both
            // require a new view selection before its command revision can advance.
            // Keep native Electric errors, including 409 must-refetch, visible for retry.
            if (reason instanceof FetchError && (reason.status === 0 || reason.status === 410)) {
              if (recoveringSelectionRef.current !== next) {
                recoveringSelectionRef.current = next;
                rotate();
              }
            } else setStreamError(displayableError(reason));
          };
          // Both shapes carry the same window, so either failing retires the same selection.
          next = {
            interest: value,
            collection: entityCollection(threadId, value, onStreamError),
            content: contentCollection(threadId, value, onStreamError),
          };
          if (selectionRef.current === null) {
            selectionRef.current = next;
            setSelection(next);
          } else {
            pendingSelectionRef.current = next;
            setPendingSelection(next);
          }
        }
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        const message = displayableError(reason);
        if (!message.includes("404")) setInterestError(message);
        retry = window.setTimeout(() => setGeneration((value) => value + 1), 1_000);
      }
    );
    return () => {
      controller.abort();
      if (retry !== undefined) window.clearTimeout(retry);
    };
  }, [beforeCursor, generation, rotate, threadId]);
  if (!selection) {
    if (interestError) return <p role="alert">Conversation sync failed: {interestError}</p>;
    return <p role="status">Loading conversation…</p>;
  }
  return (
    <>
      {interestError && (
        <p role="alert">Conversation sync failed: {interestError}; showing the current window and retrying.</p>
      )}
      {streamError && (
        <p role="alert">
          Conversation synchronization stopped: {streamError} <button onClick={rotate}>Refresh conversation</button>
        </p>
      )}
      <ActiveConversation
        threadId={threadId}
        interest={selection.interest}
        collection={selection.collection}
        content={selection.content}
        onRows={children}
        onRotate={rotate}
        role="active"
      />
      {pendingSelection && (
        <div hidden>
          <ActiveConversation
            threadId={threadId}
            interest={pendingSelection.interest}
            collection={pendingSelection.collection}
            content={pendingSelection.content}
            onRows={() => <></>}
            onRotate={rotate}
            role="pending"
            onCaughtUp={() => {
              selectionRef.current = pendingSelection;
              pendingSelectionRef.current = null;
              setSelection(pendingSelection);
              setPendingSelection(null);
            }}
          />
        </div>
      )}
    </>
  );
}

function bodyUrl(threadId: string, reference: PayloadRef): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/payload-chunks`, window.location.href);
  url.searchParams.set("source_id", reference.source_id);
  url.searchParams.set("projection_epoch", reference.projection_epoch);
  url.searchParams.set("owner_cursor", reference.owner_cursor);
  url.searchParams.set("owner_id", reference.owner_item_id);
  url.searchParams.set("field", reference.field);
  url.searchParams.set("generation", reference.generation);
  return url.toString();
}

function bodyCollection(threadId: string, reference: PayloadRef, onError: (error: unknown) => void) {
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-payload:${threadId}:${reference.source_id}:${reference.projection_epoch}:${reference.owner_cursor}:${reference.owner_item_id}:${reference.field}:${reference.generation}`,
      gcTime: 1_000,
      schema: chunkSchema,
      getKey: (row) => row.chunkIndex.toString(),
      syncMode: "eager",
      shapeOptions: { url: bodyUrl(threadId, reference), columnMapper: snakeCamelMapper(), subscribe: true, onError },
    })
  );
}

/**
 * The contiguous prefix of `generation` that `contentBytes` covers.
 *
 * A chunk is only taken while the whole of it fits, so a reader shows what its own `PayloadRef`
 * named and never a later append to the same generation that arrived ahead of the entity naming
 * it. A short prefix is what streaming looks like, not an error.
 */
function assemble(chunks: readonly PayloadChunk[], generation: bigint, contentBytes: bigint): string | null {
  const byIndex = new Map<bigint, string>();
  for (const chunk of chunks) {
    if (decimalBigInt(chunk.generation) === generation) byIndex.set(decimalBigInt(chunk.chunkIndex), chunk.text);
  }
  const parts: string[] = [];
  let bytes = 0n;
  for (let index = 0n; bytes < contentBytes; index++) {
    const text = byIndex.get(index);
    if (text === undefined) break;
    const next = bytes + BigInt(new TextEncoder().encode(text).byteLength);
    if (next > contentBytes) break;
    parts.push(text);
    bytes = next;
  }
  return bytes === 0n && contentBytes > 0n ? null : parts.join("");
}

/** Bodies the window shape already carries; anything else is read one body at a time. */
function inWindow(reference: PayloadRef): boolean {
  return reference.field === "text" || reference.field === "confirmed_input";
}

export function PayloadBody({
  threadId,
  reference,
  children,
}: {
  threadId: string;
  reference: PayloadRef;
  children: (body: string | null) => JSX.Element;
}): JSX.Element {
  const content = useContext(WindowContent);
  if (inWindow(reference) && content !== null) {
    return (
      <WindowedPayloadBody collection={content} reference={reference}>
        {children}
      </WindowedPayloadBody>
    );
  }
  return (
    <RequestedPayloadBody threadId={threadId} reference={reference}>
      {children}
    </RequestedPayloadBody>
  );
}

/**
 * Keep the last body shown for an owner's field across revisions of it.
 *
 * A replace mints a new generation whose chunks have not arrived yet, and a scope rotation rebuilds
 * every collection from empty. Both would otherwise blank text a reader is in the middle of, so the
 * previous revision stays up until its replacement has something to show.
 */
function useRetained(reference: PayloadRef, body: string | null): string | null {
  const key = `${reference.owner_item_id}:${reference.field}`;
  const retained = useRef<{ key: string; body: string } | null>(null);
  if (body !== null) retained.current = { key, body };
  else if (retained.current !== null && retained.current.key !== key) retained.current = null;
  return body ?? retained.current?.body ?? null;
}

function WindowedPayloadBody({
  collection,
  reference,
  children,
}: {
  collection: ContentCollection;
  reference: PayloadRef;
  children: (body: string | null) => JSX.Element;
}): JSX.Element {
  const query = useLiveQuery((q) => q.from({ chunk: collection }), [collection]);
  const owner = reference.owner_item_id;
  const field = reference.field;
  const chunks = useMemo(
    () => (query.data ?? []).filter((chunk) => chunk.ownerId === owner && chunk.field === field),
    [field, owner, query.data]
  );
  const body = assemble(chunks, BigInt(reference.generation), BigInt(reference.content_bytes));
  return children(useRetained(reference, body));
}

function RequestedPayloadBody({
  threadId,
  reference,
  children,
}: {
  threadId: string;
  reference: PayloadRef;
  children: (body: string | null) => JSX.Element;
}): JSX.Element {
  const refreshConversation = useContext(RefreshConversation);
  const [attempt, setAttempt] = useState(0);
  const [streamError, setStreamError] = useState<string | null>(null);
  const currentCollection = useRef<ReturnType<typeof bodyCollection> | null>(null);
  const retry = useCallback(() => {
    setStreamError(null);
    setAttempt((value) => value + 1);
  }, []);
  const key = `${reference.source_id}:${reference.projection_epoch}:${reference.owner_cursor}:${reference.owner_item_id}:${reference.field}:${reference.generation}`;
  const stable = useMemo<PayloadRef>(() => ({ ...reference }), [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const collection = useMemo(() => {
    let next: ReturnType<typeof bodyCollection>;
    next = bodyCollection(threadId, stable, (reason) => {
      if (currentCollection.current !== next) return;
      // A generation the server no longer materializes belongs to a retired scope; the
      // conversation has to be reselected before any body of it can be read again.
      if (reason instanceof FetchError && reason.status === 410) refreshConversation();
      else setStreamError(displayableError(reason));
    });
    return next;
  }, [attempt, refreshConversation, stable, threadId]);
  useEffect(() => {
    currentCollection.current = collection;
    setStreamError(null);
    return () => {
      if (currentCollection.current === collection) currentCollection.current = null;
    };
  }, [collection]);
  const query = useLiveQuery((q) => q.from({ chunk: collection }), [collection]);
  const body = assemble(query.data ?? [], BigInt(reference.generation), BigInt(reference.content_bytes));
  const retained = useRetained(reference, body);
  const stopped = streamError ?? (query.isError ? "The payload query entered an error state." : null);
  return (
    <>
      {stopped && (
        <p role="alert">
          Payload synchronization stopped: {stopped} <button onClick={retry}>Retry payload synchronization</button>
        </p>
      )}
      {children(retained)}
    </>
  );
}
