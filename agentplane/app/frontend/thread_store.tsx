import { FetchError, snakeCamelMapper } from "@electric-sql/client";
import { electricCollectionOptions } from "@tanstack/electric-db-collection";
import { createCollection, useLiveQuery } from "@tanstack/react-db";
import { createContext, type JSX, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";

import { threadEntityInterest, displayableError, type ThreadEntityView, type EntityInterest } from "./client";

const decimal: z.ZodType<string | bigint> = z.union([z.string().regex(/^-?\d+$/), z.bigint()]);
const RefreshThread = createContext<() => void>(() => undefined);
declare global {
  interface Window {
    __agentplaneThreadCollectionTrace?: unknown[];
  }
}
type Decimal = z.output<typeof decimal>;
export function decimalBigInt(value: Decimal): bigint {
  return typeof value === "bigint" ? value : BigInt(value);
}
export type PayloadRef = NonNullable<ThreadEntityView["text_ref"]>;
type ThreadEntityState = ThreadEntityView["state"];

export interface ThreadEntity {
  threadId: string;
  projectionEpoch: string;
  entityKind: "view_state" | "item" | "confirmed_input" | "lifecycle" | "command";
  entityId: string;
  cursor: Decimal;
  revisionCursor: Decimal;
  pending: boolean;
  turnId: string | null;
  state: ThreadEntityState;
  textRef: PayloadRef | null;
  argumentsRef: PayloadRef | null;
  outputRef: PayloadRef | null;
  inputRef: PayloadRef | null;
}
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
  projectionEpoch: z.string(),
  ownerCursor: decimal,
  ownerId: z.string(),
  field: z.string(),
  generation: decimal,
  chunkIndex: decimal,
  text: z.string(),
});
type PayloadChunk = z.output<typeof chunkSchema>;

function entityUrl(threadId: string, interest: EntityInterest): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/entities`, window.location.href);
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
      id: `agentplane-thread:${threadId}:${interest.projection_epoch}:${interest.anchor_cursor}:${interest.window_from ?? "tail"}`,
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
  const trace = window.__agentplaneThreadCollectionTrace;
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

function commandUrl(threadId: string, projectionEpoch: string, commandIds: readonly string[]): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/commands`, window.location.href);
  url.searchParams.set("projection_epoch", projectionEpoch);
  for (const id of [...new Set(commandIds)].sort()) url.searchParams.append("command_id", id);
  return url.toString();
}

function commandCollection(
  threadId: string,
  projectionEpoch: string,
  commandIds: readonly string[],
  onError: (error: unknown) => void
) {
  const selected = [...new Set(commandIds)].sort();
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-commands:${threadId}:${projectionEpoch}:${selected.join(":")}`,
      gcTime: 1_000,
      schema: entitySchema,
      getKey: (row) => row.entityId,
      syncMode: "on-demand",
      shapeOptions: {
        url: commandUrl(threadId, projectionEpoch, selected),
        params: { log: "changes_only" },
        columnMapper: snakeCamelMapper(),
        onError,
      },
    })
  );
}

export function CommandSelection({
  threadId,
  projectionEpoch,
  commandIds,
  children,
}: {
  threadId: string;
  projectionEpoch: string;
  commandIds: readonly string[];
  children: (rows: ThreadEntity[]) => JSX.Element;
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
    next = commandCollection(threadId, projectionEpoch, key.split("\u0000"), (reason) => {
      if (currentCollection.current !== next) return;
      setStreamError({ collection: next, message: displayableError(reason) });
    });
    return next;
  }, [attempt, key, projectionEpoch, threadId]);
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

function ActiveCollection({
  threadId,
  interest,
  collection,
  onRows,
  onRotate,
  onCaughtUp,
  role,
}: {
  threadId: string;
  interest: EntityInterest;
  collection: ReturnType<typeof entityCollection>;
  onRows: (rows: ThreadEntity[], interest: EntityInterest) => JSX.Element;
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
    <RefreshThread.Provider value={onRotate}>
      {query.isError && <p role="alert">Thread synchronization stopped.</p>}
      {!query.isError && !caughtUp && (
        <p role="status" data-thread-catchup="true">
          Catching up thread…
        </p>
      )}
      {onRows(caughtUp ? rows : [], interest)}
    </RefreshThread.Provider>
  );
}

export function ThreadCollection({
  threadId,
  beforeCursor,
  children,
}: {
  threadId: string;
  beforeCursor?: string;
  children: (rows: ThreadEntity[], interest: EntityInterest) => JSX.Element;
}): JSX.Element {
  type Selection = { interest: EntityInterest; collection: ReturnType<typeof entityCollection> };
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
    void threadEntityInterest(threadId, beforeCursor, controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) {
          const next: Selection = {
            interest: value,
            collection: entityCollection(threadId, value, (reason) => {
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
            }),
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
    if (interestError) return <p role="alert">Thread sync failed: {interestError}</p>;
    return <p role="status">Loading thread…</p>;
  }
  return (
    <>
      {interestError && (
        <p role="alert">Thread sync failed: {interestError}; showing the current window and retrying.</p>
      )}
      {streamError && (
        <p role="alert">
          Thread synchronization stopped: {streamError} <button onClick={rotate}>Refresh thread</button>
        </p>
      )}
      <ActiveCollection
        threadId={threadId}
        interest={selection.interest}
        collection={selection.collection}
        onRows={children}
        onRotate={rotate}
        role="active"
      />
      {pendingSelection && (
        <div hidden>
          <ActiveCollection
            threadId={threadId}
            interest={pendingSelection.interest}
            collection={pendingSelection.collection}
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

function chunkUrl(threadId: string, reference: PayloadRef, follow: boolean): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/payload-chunks`, window.location.href);
  url.searchParams.set("projection_epoch", reference.projection_epoch);
  url.searchParams.set("owner_cursor", reference.owner_cursor);
  url.searchParams.set("owner_id", reference.owner_id);
  url.searchParams.set("field", reference.field);
  url.searchParams.set("generation", reference.generation);
  url.searchParams.set("revision_cursor", reference.revision_cursor);
  if (follow) url.searchParams.set("follow", "true");
  return url.toString();
}

function chunkCollection(threadId: string, reference: PayloadRef, follow: boolean, onError: (error: unknown) => void) {
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-payload:${threadId}:${reference.projection_epoch}:${reference.owner_cursor}:${reference.owner_id}:${reference.field}:${reference.generation}:${follow ? "follow" : reference.revision_cursor}`,
      gcTime: 1_000,
      schema: chunkSchema,
      getKey: (row) => row.chunkIndex.toString(),
      syncMode: "eager",
      shapeOptions: {
        url: chunkUrl(threadId, reference, follow),
        columnMapper: snakeCamelMapper(),
        subscribe: follow,
        onError,
      },
    })
  );
}

export function PayloadBody({
  threadId,
  reference,
  follow,
  children,
}: {
  threadId: string;
  reference: PayloadRef;
  follow: boolean;
  children: (body: string | null) => JSX.Element;
}): JSX.Element {
  const refreshThread = useContext(RefreshThread);
  const referenceKey = `${reference.projection_epoch}:${reference.owner_cursor}:${reference.owner_id}:${reference.field}:${reference.generation}:${reference.revision_cursor}`;
  const stableReference = useMemo<PayloadRef>(
    () => ({
      projection_epoch: reference.projection_epoch,
      owner_cursor: reference.owner_cursor,
      owner_id: reference.owner_id,
      field: reference.field,
      generation: reference.generation,
      revision_cursor: reference.revision_cursor,
    }),
    [
      reference.field,
      reference.generation,
      reference.owner_cursor,
      reference.owner_id,
      reference.projection_epoch,
      reference.revision_cursor,
    ]
  );
  const [selection, setSelection] = useState<{
    key: string;
    reference: PayloadRef;
    follow: boolean;
    chunkCount: string;
    contentBytes: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const refreshPayload = useCallback(() => {
    setRefreshGeneration((value) => value + 1);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/payload-interest`, window.location.href);
    url.search = new URL(chunkUrl(threadId, stableReference, false)).search;
    let retry: number | undefined;
    let attempt = 0;
    const load = (): void => {
      if (retry !== undefined) window.clearTimeout(retry);
      void fetch(url, { signal: controller.signal })
        .then(async (response) => {
          if (response.status === 410) {
            refreshThread();
            throw new Error("Payload revision is unavailable after thread reset");
          }
          if (!response.ok) throw new Error(`Payload selection failed with ${response.status}`);
          const value = (await response.json()) as { chunk_count: string; content_bytes: string };
          if (!controller.signal.aborted) {
            setSelection({
              key: referenceKey,
              reference: stableReference,
              follow,
              chunkCount: value.chunk_count,
              contentBytes: value.content_bytes,
            });
            setError(null);
          }
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          setError(displayableError(reason));
          retry = window.setTimeout(load, Math.min(5_000, 250 * 2 ** attempt++));
        });
    };
    const online = (): void => {
      if (!controller.signal.aborted) load();
    };
    window.addEventListener("online", online);
    load();
    return () => {
      controller.abort();
      if (retry !== undefined) window.clearTimeout(retry);
      window.removeEventListener("online", online);
    };
  }, [follow, referenceKey, refreshThread, refreshGeneration, stableReference, threadId]);
  const sameScope = selection?.reference.projection_epoch === reference.projection_epoch;
  if (!selection || !sameScope) return error ? <p role="alert">{error}</p> : children(null);
  return (
    <>
      {selection.key !== referenceKey && <p role="status">Loading newer revision; showing previous revision.</p>}
      {error && <p role="alert">{error}; retrying.</p>}
      <ActivePayloadBody
        threadId={threadId}
        reference={selection.reference}
        extent={selection}
        follow={selection.follow}
        refreshGeneration={refreshGeneration}
        onRetry={refreshPayload}
      >
        {children}
      </ActivePayloadBody>
    </>
  );
}

function ActivePayloadBody({
  threadId,
  reference,
  extent,
  follow,
  refreshGeneration,
  onRetry,
  children,
}: {
  threadId: string;
  reference: PayloadRef;
  extent: { chunkCount: string; contentBytes: string } | null;
  follow: boolean;
  refreshGeneration: number;
  onRetry: () => void;
  children: (body: string | null) => JSX.Element;
}): JSX.Element {
  const selectedRevision = follow ? "follow" : reference.revision_cursor;
  const currentCollection = useRef<ReturnType<typeof chunkCollection> | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const retry = useCallback(() => {
    setStreamError(null);
    onRetry();
  }, [onRetry]);
  const collection = useMemo(() => {
    let next: ReturnType<typeof chunkCollection>;
    next = chunkCollection(
      threadId,
      {
        projection_epoch: reference.projection_epoch,
        owner_cursor: reference.owner_cursor,
        owner_id: reference.owner_id,
        field: reference.field,
        generation: reference.generation,
        revision_cursor: follow ? reference.revision_cursor : selectedRevision,
      },
      follow,
      (reason) => {
        if (currentCollection.current === next) setStreamError(displayableError(reason));
      }
    );
    return next;
  }, [
    follow,
    reference.field,
    reference.generation,
    reference.owner_cursor,
    reference.owner_id,
    reference.projection_epoch,
    refreshGeneration,
    selectedRevision,
    threadId,
  ]);
  useEffect(() => {
    currentCollection.current = collection;
    setStreamError(null);
    return () => {
      if (currentCollection.current === collection) currentCollection.current = null;
    };
  }, [collection]);
  const query = useLiveQuery((q) => q.from({ chunk: collection }), [collection]);
  const stopped = streamError ?? (query.isError ? "The payload query entered an error state." : null);
  if (!extent) return children(null);
  const expected = BigInt(extent.chunkCount);
  const chunks = (query.data ?? [])
    .filter((chunk) => decimalBigInt(chunk.chunkIndex) < expected)
    .sort((left, right) =>
      decimalBigInt(left.chunkIndex) < decimalBigInt(right.chunkIndex)
        ? -1
        : decimalBigInt(left.chunkIndex) > decimalBigInt(right.chunkIndex)
          ? 1
          : 0
    );
  const contiguous =
    chunks.length === Number(expected) &&
    chunks.every((chunk, index) => decimalBigInt(chunk.chunkIndex) === BigInt(index));
  const body = contiguous ? chunks.map((chunk: PayloadChunk) => chunk.text).join("") : null;
  const complete = body !== null && BigInt(new TextEncoder().encode(body).byteLength) === BigInt(extent.contentBytes);
  return (
    <>
      {stopped && (
        <p role="alert">
          Payload synchronization stopped: {stopped} <button onClick={retry}>Retry payload synchronization</button>
        </p>
      )}
      {children(complete ? body : null)}
    </>
  );
}
