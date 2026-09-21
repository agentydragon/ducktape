import { snakeCamelMapper } from "@electric-sql/client";
import { electricCollectionOptions } from "@tanstack/electric-db-collection";
import { createCollection, useLiveQuery } from "@tanstack/react-db";
import { createContext, type JSX, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { z } from "zod";

import { conversationInterest, displayableError, type ConversationStoredEntity, type EntityInterest } from "./client";

const decimal: z.ZodType<string | bigint> = z.union([z.string().regex(/^-?\d+$/), z.bigint()]);
const RefreshConversation = createContext<() => void>(() => undefined);
type Decimal = z.output<typeof decimal>;
export function decimalBigInt(value: Decimal): bigint {
  return typeof value === "bigint" ? value : BigInt(value);
}
export type PayloadRef = NonNullable<ConversationStoredEntity["text_ref"]>;
type StoredState = ConversationStoredEntity["state"];
type StoredViewState = Extract<StoredState, { controls: unknown }>;
type ConversationState =
  | Exclude<StoredState, StoredViewState>
  | (StoredViewState & {
      operational: {
        operational_version: string;
        status: "active" | "ended" | "failed";
        last_verified_cursor: string;
        feed_error: { cursor: string; message: string } | null;
      };
    });

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
      feed_error: z.object({ cursor: z.string(), message: z.string() }).nullable(),
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

function entityUrl(threadId: string, interest: EntityInterest): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/entities`, window.location.href);
  url.searchParams.set("anchor_cursor", interest.anchor_cursor);
  url.searchParams.set("tail_from", interest.tail_from);
  if (interest.window_from !== null) url.searchParams.set("window_from", interest.window_from);
  if (interest.window_before !== null) url.searchParams.set("window_before", interest.window_before);
  return url.toString();
}

function entityCollection(threadId: string, interest: EntityInterest) {
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-conversation:${threadId}:${interest.anchor_cursor}:${interest.window_from ?? "tail"}`,
      gcTime: 1_000,
      schema: entitySchema,
      getKey: (row) => `${row.entityKind}:${row.entityId}`,
      syncMode: "eager",
      shapeOptions: {
        url: entityUrl(threadId, interest),
        columnMapper: snakeCamelMapper(),
      },
    })
  );
}

function ActiveConversation({
  threadId,
  interest,
  onRows,
  onRotate,
}: {
  threadId: string;
  interest: EntityInterest;
  onRows: (rows: ConversationEntity[], interest: EntityInterest) => JSX.Element;
  onRotate: () => void;
}): JSX.Element {
  const collection = useMemo(() => entityCollection(threadId, interest), [threadId, interest]);
  const query = useLiveQuery((q) => q.from({ entity: collection }), [collection]);
  const rows = query.data ?? [];
  const view = rows.find((row) => row.entityKind === "view_state");
  const caughtUp = view !== undefined && decimalBigInt(view.revisionCursor) >= BigInt(interest.through_cursor);
  const segmentCount = rows.filter((row) => ["item", "confirmed_input", "lifecycle"].includes(row.entityKind)).length;
  useEffect(() => {
    if (segmentCount > 60) onRotate();
  }, [onRotate, segmentCount]);
  useEffect(() => {
    if (!query.isError) return;
    const retry = window.setTimeout(onRotate, 1_000);
    return () => window.clearTimeout(retry);
  }, [onRotate, query.isError]);
  return (
    <RefreshConversation.Provider value={onRotate}>
      {query.isError && <p role="alert">Conversation synchronization stopped.</p>}
      {!query.isError && !caughtUp && <p role="status">Catching up conversation…</p>}
      {onRows(caughtUp ? rows : [], interest)}
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
  const [interest, setInterest] = useState<EntityInterest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const rotate = useCallback(() => setGeneration((value) => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    let retry: number | undefined;
    setError(null);
    void conversationInterest(threadId, beforeCursor, controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) setInterest(value);
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        const message = displayableError(reason);
        if (message.includes("404")) retry = window.setTimeout(() => setGeneration((value) => value + 1), 1000);
        else setError(message);
      }
    );
    return () => {
      controller.abort();
      if (retry !== undefined) window.clearTimeout(retry);
    };
  }, [beforeCursor, generation, threadId]);
  if (error) return <p role="alert">Conversation sync failed: {error}</p>;
  if (!interest) return <p role="status">Loading conversation…</p>;
  return <ActiveConversation threadId={threadId} interest={interest} onRows={children} onRotate={rotate} />;
}

function chunkUrl(threadId: string, reference: PayloadRef, follow: boolean): string {
  const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/payload-chunks`, window.location.href);
  url.searchParams.set("source_id", reference.source_id);
  url.searchParams.set("projection_epoch", reference.projection_epoch);
  url.searchParams.set("owner_cursor", reference.owner_cursor);
  url.searchParams.set("owner_id", reference.owner_item_id);
  url.searchParams.set("field", reference.field);
  url.searchParams.set("generation", reference.generation);
  url.searchParams.set("revision_cursor", reference.revision_cursor);
  if (follow) url.searchParams.set("follow", "true");
  return url.toString();
}

function chunkCollection(threadId: string, reference: PayloadRef, follow: boolean) {
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-payload:${threadId}:${reference.owner_item_id}:${reference.field}:${reference.generation}:${follow}`,
      gcTime: 1_000,
      schema: chunkSchema,
      getKey: (row) => row.chunkIndex.toString(),
      syncMode: "eager",
      shapeOptions: {
        url: chunkUrl(threadId, reference, follow),
        columnMapper: snakeCamelMapper(),
        subscribe: follow,
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
  const refreshConversation = useContext(RefreshConversation);
  const referenceKey = `${reference.source_id}:${reference.projection_epoch}:${reference.owner_cursor}:${reference.owner_item_id}:${reference.field}:${reference.generation}:${reference.revision_cursor}`;
  const [selection, setSelection] = useState<{
    key: string;
    reference: PayloadRef;
    follow: boolean;
    chunkCount: string;
    contentBytes: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    const url = new URL(`/threads/${encodeURIComponent(threadId)}/sync/payload-interest`, window.location.href);
    url.search = new URL(chunkUrl(threadId, reference, false)).search;
    let retry: number | undefined;
    let attempt = 0;
    const load = (): void => {
      if (retry !== undefined) window.clearTimeout(retry);
      void fetch(url, { signal: controller.signal })
        .then(async (response) => {
          if (response.status === 410) {
            refreshConversation();
            throw new Error("Payload revision is unavailable after conversation reset");
          }
          if (!response.ok) throw new Error(`Payload selection failed with ${response.status}`);
          const value = (await response.json()) as { chunk_count: string; content_bytes: string };
          if (!controller.signal.aborted) {
            setSelection({
              key: referenceKey,
              reference,
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
  }, [follow, reference, referenceKey, refreshConversation, threadId]);
  const sameScope =
    selection?.reference.source_id === reference.source_id &&
    selection.reference.projection_epoch === reference.projection_epoch;
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
  children,
}: {
  threadId: string;
  reference: PayloadRef;
  extent: { chunkCount: string; contentBytes: string } | null;
  follow: boolean;
  children: (body: string | null) => JSX.Element;
}): JSX.Element {
  const selectedRevision = follow ? "follow" : reference.revision_cursor;
  const collection = useMemo(
    () =>
      chunkCollection(
        threadId,
        {
          source_id: reference.source_id,
          projection_epoch: reference.projection_epoch,
          owner_cursor: reference.owner_cursor,
          owner_item_id: reference.owner_item_id,
          field: reference.field,
          generation: reference.generation,
          revision_cursor: follow ? reference.revision_cursor : selectedRevision,
        },
        follow
      ),
    [
      follow,
      reference.field,
      reference.generation,
      reference.owner_cursor,
      reference.owner_item_id,
      reference.projection_epoch,
      reference.source_id,
      selectedRevision,
      threadId,
    ]
  );
  const query = useLiveQuery((q) => q.from({ chunk: collection }), [collection]);
  if (query.isError) return <p role="alert">Payload synchronization stopped.</p>;
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
  return children(complete ? body : null);
}
