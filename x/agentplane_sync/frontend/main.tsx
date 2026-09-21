import { snakeCamelMapper } from "@electric-sql/client";
import { electricCollectionOptions } from "@tanstack/electric-db-collection";
import { createCollection, eq, lt, useLiveQuery } from "@tanstack/react-db";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { z } from "zod";

const generationIdSchema = z.union([z.string(), z.bigint()]).transform((value) => value.toString());

const viewRowSchema = z.object({
  conversationId: z.string(),
  rowKey: z.string(),
  entityKind: z.string(),
  anchor: z.coerce.bigint(),
  revision: z.coerce.bigint(),
  itemId: z.string().nullable(),
  itemKind: z.number().nullable(),
  toolName: z.string().nullable(),
  textRevision: z.coerce.bigint(),
  argumentsRevision: z.coerce.bigint(),
  outputRevision: z.coerce.bigint(),
  reasoningRevision: z.coerce.bigint(),
  textBytes: z.coerce.bigint(),
  argumentsBytes: z.coerce.bigint(),
  outputBytes: z.coerce.bigint(),
  reasoningBytes: z.coerce.bigint(),
  textPayloadRef: z.string().nullable(),
  argumentsPayloadRef: z.string().nullable(),
  outputPayloadRef: z.string().nullable(),
  reasoningPayloadRef: z.string().nullable(),
  textGenerationId: generationIdSchema.nullable(),
  argumentsGenerationId: generationIdSchema.nullable(),
  outputGenerationId: generationIdSchema.nullable(),
  reasoningGenerationId: generationIdSchema.nullable(),
  textChunkCount: z.number().int().nonnegative(),
  argumentsChunkCount: z.number().int().nonnegative(),
  outputChunkCount: z.number().int().nonnegative(),
  reasoningChunkCount: z.number().int().nonnegative(),
  status: z.string().nullable(),
  model: z.string().nullable(),
  commandId: z.string().nullable(),
});

type ViewRow = z.output<typeof viewRowSchema>;
type PayloadField = "text" | "arguments" | "output" | "reasoning";
type PayloadInterest = {
  itemId: string;
  field: PayloadField;
  payloadRef: string | null;
  generationId: string | null;
  shapeRef: string | null;
  followLatest: boolean;
  revision: bigint;
  chunkCount: number;
  contentBytes: bigint;
  epoch: number;
};
type ActivePayloadInterest = PayloadInterest & { payloadRef: string; generationId: string; shapeRef: string };

function isActivePayloadInterest(selection: PayloadInterest): selection is ActivePayloadInterest {
  return selection.payloadRef !== null && selection.generationId !== null && selection.shapeRef !== null;
}

async function sha256Text(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

const payloadManifestSchema = z.object({
  payloadRef: z.string(),
  conversationId: z.string(),
  itemId: z.string(),
  fieldName: z.enum(["text", "arguments", "output", "reasoning"]),
  sourceId: z.string(),
  generationId: generationIdSchema,
  revision: z.coerce.bigint(),
  present: z.boolean(),
  chunkCount: z.number().int().nonnegative(),
  contentBytes: z.coerce.bigint(),
  sourceCursor: z.coerce.bigint(),
});

const payloadChunkSchema = z.object({
  conversationId: z.string(),
  itemId: z.string(),
  fieldName: z.enum(["text", "arguments", "output", "reasoning"]),
  sourceId: z.string(),
  generationId: generationIdSchema,
  chunkIndex: z.number().int().nonnegative(),
  sourceCursor: z.coerce.bigint(),
  content: z.string(),
  contentBytes: z.coerce.bigint(),
});

type PayloadManifest = z.output<typeof payloadManifestSchema>;

declare global {
  interface Window {
    __syncEvidence: {
      atomicStates: string[][];
      revisions: Record<string, string[]>;
      rowKeys: string[];
      exactBigint: boolean;
      payloadStates: Array<{
        itemId: string;
        field: PayloadField;
        epoch: number;
        payloadRef: string;
        revision: string;
        latestRef: string;
        contentSha256: string;
        chunkCount: number;
        contentBytes: string;
      }>;
      retiredPayloadCollections: Array<{
        shapeRef: string;
        collectionId: string;
        subscribersAfterCleanup: number;
        sizeAfterCleanup: number;
        statusAfterCleanup: string;
        automaticGc: boolean;
        cleanupElapsedMs: number;
      }>;
      scroll: Array<{
        key: string;
        beforeTop: number;
        preCorrectionTop: number;
        afterTop: number;
        correctionPx: number;
        delta: number;
      }>;
      pageErrors: string[];
    };
  }
}

window.__syncEvidence = {
  atomicStates: [],
  revisions: {},
  rowKeys: [],
  exactBigint: false,
  payloadStates: [],
  retiredPayloadCollections: [],
  scroll: [],
  pageErrors: [],
};

function makeRows(conversationId: string) {
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-view:${conversationId}`,
      schema: viewRowSchema,
      getKey: (row) => row.rowKey,
      syncMode: "on-demand",
      shapeOptions: {
        url: new URL(`/api/electric/${encodeURIComponent(conversationId)}`, window.location.href).toString(),
        params: { log: "changes_only", replica: "full" },
        headers: { Authorization: `Bearer ${localStorage.getItem("spike-token") ?? ""}` },
        columnMapper: snakeCamelMapper(),
      },
    })
  );
}

function makePayloadChunks(conversationId: string, payloadRef: string, epoch: number, followLatest: boolean) {
  return createCollection(
    electricCollectionOptions({
      id: `agentplane-payload-chunks:${conversationId}:${payloadRef}:${epoch}`,
      schema: payloadChunkSchema,
      getKey: (row) => `${row.sourceId}:${row.generationId}:${row.chunkIndex}`,
      // Let TanStack's GC stop this shape after the query's source subscription
      // retires. Calling cleanup() directly from React effect cleanup races that
      // subscription and leaves the live query in an error state.
      gcTime: 1,
      syncMode: followLatest ? "eager" : "on-demand",
      shapeOptions: {
        url: new URL(
          `/api/electric/${encodeURIComponent(conversationId)}/payload/${encodeURIComponent(payloadRef)}/${
            followLatest ? "chunks" : "revision"
          }`,
          window.location.href
        ).toString(),
        params: { replica: "full" },
        headers: { Authorization: `Bearer ${localStorage.getItem("spike-token") ?? ""}` },
        columnMapper: snakeCamelMapper(),
      },
    })
  );
}

function payloadRef(row: ViewRow | undefined, field: PayloadField): string | null {
  if (!row) return null;
  switch (field) {
    case "text":
      return row.textPayloadRef;
    case "arguments":
      return row.argumentsPayloadRef;
    case "output":
      return row.outputPayloadRef;
    case "reasoning":
      return row.reasoningPayloadRef;
  }
}

function payloadGeneration(row: ViewRow | undefined, field: PayloadField): string | null {
  if (!row) return null;
  switch (field) {
    case "text":
      return row.textGenerationId === null ? null : String(row.textGenerationId);
    case "arguments":
      return row.argumentsGenerationId === null ? null : String(row.argumentsGenerationId);
    case "output":
      return row.outputGenerationId === null ? null : String(row.outputGenerationId);
    case "reasoning":
      return row.reasoningGenerationId === null ? null : String(row.reasoningGenerationId);
  }
}

function payloadRevision(row: ViewRow | undefined, field: PayloadField): bigint {
  if (!row) return 0n;
  switch (field) {
    case "text":
      return row.textRevision;
    case "arguments":
      return row.argumentsRevision;
    case "output":
      return row.outputRevision;
    case "reasoning":
      return row.reasoningRevision;
  }
}

function payloadChunkCount(row: ViewRow | undefined, field: PayloadField): number {
  if (!row) return 0;
  switch (field) {
    case "text":
      return row.textChunkCount;
    case "arguments":
      return row.argumentsChunkCount;
    case "output":
      return row.outputChunkCount;
    case "reasoning":
      return row.reasoningChunkCount;
  }
}

function payloadContentBytes(row: ViewRow | undefined, field: PayloadField): bigint {
  if (!row) return 0n;
  switch (field) {
    case "text":
      return row.textBytes;
    case "arguments":
      return row.argumentsBytes;
    case "output":
      return row.outputBytes;
    case "reasoning":
      return row.reasoningBytes;
  }
}

function PayloadPanel({
  conversationId,
  selection,
  latestRef,
  onReady,
}: {
  conversationId: string;
  selection: ActivePayloadInterest;
  latestRef: string | null;
  onReady: (epoch: number, payloadRef: string) => void;
}) {
  const selectedRef = selection.payloadRef;
  const chunksCollection = useMemo(
    () => makePayloadChunks(conversationId, selection.shapeRef, selection.epoch, selection.followLatest),
    [conversationId, selection.epoch, selection.followLatest, selection.shapeRef]
  );
  useEffect(() => {
    const collection = chunksCollection;
    const shapeRef = selection.shapeRef;
    return () => {
      const closedAt = performance.now();
      const recordGc = () => {
        const status = collection.status;
        const automaticGc = status === "cleaned-up";
        if (automaticGc || performance.now() - closedAt >= 10_000) {
          window.__syncEvidence.retiredPayloadCollections.push({
            shapeRef,
            collectionId: collection.id,
            subscribersAfterCleanup: collection.subscriberCount,
            sizeAfterCleanup: collection.size,
            statusAfterCleanup: status,
            automaticGc,
            cleanupElapsedMs: Number((performance.now() - closedAt).toFixed(1)),
          });
          return;
        }
        // The derived useLiveQuery collection owns this collection until its
        // own no-subscriber GC runs. Poll after unmount rather than cleaning up
        // a source that still has a live query dependent.
        window.setTimeout(recordGc, 10);
      };
      window.setTimeout(recordGc, 10);
    };
  }, [chunksCollection, selection.shapeRef]);
  const chunksQuery = useLiveQuery((q) => q.from({ chunk: chunksCollection }), [chunksCollection]);
  const chunks = chunksQuery.data ?? [];
  const [manifest, setManifest] = useState<PayloadManifest | null>(null);
  const [manifestState, setManifestState] = useState<"loading" | "ready" | "error">("loading");
  const [manifestError, setManifestError] = useState("");
  const reported = useRef(new Set<string>());

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setManifest(null);
    setManifestState("loading");
    setManifestError("");
    const token = localStorage.getItem("spike-token") ?? "";
    void fetch(`/api/payloads/${encodeURIComponent(conversationId)}/${encodeURIComponent(selectedRef)}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 410) throw new Error("Payload revision is unavailable or expired");
        if (!response.ok) throw new Error(`Payload manifest request failed with ${response.status}`);
        return payloadManifestSchema.parse(await response.json());
      })
      .then((value) => {
        if (!current) return;
        const expected = {
          payloadRef: selectedRef,
          conversationId,
          itemId: selection.itemId,
          fieldName: selection.field,
          generationId: selection.generationId,
          revision: selection.revision.toString(),
          sourceCursor: selection.revision.toString(),
          chunkCount: selection.chunkCount,
          contentBytes: selection.contentBytes.toString(),
        };
        const actual = {
          payloadRef: value.payloadRef,
          conversationId: value.conversationId,
          itemId: value.itemId,
          fieldName: value.fieldName,
          generationId: value.generationId,
          revision: value.revision.toString(),
          sourceCursor: value.sourceCursor.toString(),
          chunkCount: value.chunkCount,
          contentBytes: value.contentBytes.toString(),
        };
        const mismatch = Object.fromEntries(
          Object.keys(expected)
            .filter((key) => expected[key as keyof typeof expected] !== actual[key as keyof typeof actual])
            .map((key) => [
              key,
              { expected: expected[key as keyof typeof expected], actual: actual[key as keyof typeof actual] },
            ])
        );
        if (Object.keys(mismatch).length > 0) {
          const details = JSON.stringify(mismatch, (_key, fieldValue: unknown) =>
            typeof fieldValue === "bigint" ? fieldValue.toString() : fieldValue
          );
          throw new Error(`Payload manifest does not match selected row revision: ${details}`);
        }
        setManifest(value);
        setManifestState("ready");
      })
      .catch((error: unknown) => {
        if (!current || (error instanceof DOMException && error.name === "AbortError")) return;
        setManifestError(String(error));
        setManifestState("error");
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [
    conversationId,
    selectedRef,
    selection.epoch,
    selection.field,
    selection.generationId,
    selection.itemId,
    selection.revision,
    selection.chunkCount,
    selection.contentBytes,
  ]);

  const selectedChunks = manifest
    ? chunks
        .filter(
          (chunk) =>
            chunk.conversationId === conversationId &&
            chunk.itemId === selection.itemId &&
            chunk.fieldName === selection.field &&
            chunk.sourceId === manifest.sourceId &&
            String(chunk.generationId) === manifest.generationId &&
            chunk.chunkIndex < manifest.chunkCount &&
            chunk.sourceCursor <= manifest.sourceCursor
        )
        .sort((left, right) => left.chunkIndex - right.chunkIndex)
    : [];
  const complete =
    manifest !== null &&
    manifest.payloadRef === selectedRef &&
    manifest.generationId === selection.generationId &&
    manifest.present &&
    selectedChunks.length === manifest.chunkCount &&
    selectedChunks.every((chunk, index) => chunk.chunkIndex === index) &&
    selectedChunks.reduce((total, chunk) => total + chunk.contentBytes, 0n) === manifest.contentBytes;
  const status =
    manifestState === "error" || chunksQuery.isError
      ? "error"
      : complete
        ? "ready"
        : manifestState === "ready"
          ? "hydrating"
          : "loading";

  useLayoutEffect(() => {
    if (!complete || manifest === null) return;
    const key = `${selection.epoch}:${manifest.payloadRef}:${manifest.revision}`;
    if (reported.current.has(key)) return;
    reported.current.add(key);
    const element = document.querySelector<HTMLOutputElement>("[data-testid=payload-body]");
    if (!element) return;
    const content = element.textContent ?? "";
    let active = true;
    void sha256Text(content).then((contentSha256) => {
      if (!active) return;
      window.__syncEvidence.payloadStates.push({
        itemId: element.dataset.itemId ?? "",
        field: element.dataset.field as PayloadField,
        epoch: Number(element.dataset.epoch),
        payloadRef: element.dataset.payloadRef ?? "",
        revision: element.dataset.revision ?? "",
        latestRef: element.dataset.latestRef ?? "",
        contentSha256,
        chunkCount: Number(element.dataset.chunkCount),
        contentBytes: element.dataset.contentBytes ?? "",
      });
      onReady(selection.epoch, manifest.payloadRef);
    });
    return () => {
      active = false;
    };
  }, [complete, manifest, onReady, selection.epoch]);

  return (
    <output
      data-testid="payload-body"
      data-state={status}
      data-item-id={selection.itemId}
      data-field={selection.field}
      data-epoch={selection.epoch}
      data-payload-ref={manifest?.payloadRef ?? selectedRef}
      data-revision={manifest?.revision.toString() ?? ""}
      data-latest-ref={latestRef ?? ""}
      data-chunk-count={manifest?.chunkCount ?? ""}
      data-content-bytes={manifest?.contentBytes.toString() ?? ""}
      data-shape-ref={selection.shapeRef}
      title={status === "error" ? manifestError || String(chunksQuery.status) : undefined}
    >
      {status === "error"
        ? manifestError || "Payload chunks could not be synchronized"
        : complete
          ? selectedChunks.map((chunk) => <span key={chunk.chunkIndex}>{chunk.content}</span>)
          : null}
    </output>
  );
}

function HistoryPage({
  collection,
  before,
  pageId,
  onRows,
}: {
  collection: ReturnType<typeof makeRows>;
  before: bigint;
  pageId: string;
  onRows: (pageId: string, rows: ViewRow[]) => void;
}) {
  const query = useLiveQuery(
    (q) =>
      q
        .from({ row: collection })
        .where(({ row }) => lt(row.anchor, before))
        .where(({ row }) => eq(row.entityKind, "item"))
        .orderBy(({ row }) => row.anchor, "desc")
        .limit(30),
    [collection, before]
  );
  const rows = query.data ?? [];
  useEffect(() => onRows(pageId, rows), [pageId, rows, onRows]);
  useEffect(() => {
    for (const row of rows) {
      const revisions = (window.__syncEvidence.revisions[row.rowKey] ??= []);
      const latest = row.revision.toString();
      if (revisions.at(-1) !== latest) revisions.push(latest);
    }
  }, [rows]);
  return null;
}

function App() {
  const query = new URLSearchParams(location.search);
  const conversationId = query.get("conversation") ?? "alpha-large";
  const payloadItem = query.get("payloadItem") ?? "live-item";
  const pinnedPayloadRef = query.get("payloadRef");
  const collection = useMemo(() => makeRows(conversationId), [conversationId]);
  const tail = useLiveQuery(
    (q) =>
      q
        .from({ row: collection })
        .orderBy(({ row }) => row.anchor, "desc")
        .limit(30),
    [collection]
  );
  const tailRows = tail.data ?? [];
  const [historyCursors, setHistoryCursors] = useState<bigint[]>([]);
  const [historyByPage, setHistoryByPage] = useState<Record<string, ViewRow[]>>({});
  const [interest, setInterest] = useState<PayloadInterest | null>(null);
  const [payloadReady, setPayloadReady] = useState<{ epoch: number; payloadRef: string } | null>(null);
  const nextPayloadEpoch = useRef(0);
  const loadedPinnedRef = useRef(false);
  const viewport = useRef<HTMLDivElement>(null);
  const pendingScroll = useRef<{ key: string; top: number; rowCount: number } | null>(null);
  const receiveHistory = useCallback((pageId: string, rows: ViewRow[]) => {
    setHistoryByPage((current) => (current[pageId] === rows ? current : { ...current, [pageId]: rows }));
  }, []);
  const allRows = useMemo(() => {
    const unique = new Map<string, ViewRow>();
    for (const row of tailRows) unique.set(row.rowKey, row);
    for (const rows of Object.values(historyByPage)) for (const row of rows) unique.set(row.rowKey, row);
    return [...unique.values()].sort((left, right) =>
      left.anchor < right.anchor ? -1 : left.anchor > right.anchor ? 1 : 0
    );
  }, [tailRows, historyByPage]);
  const toolRow = allRows.find((row) => row.rowKey === "item:tool-row");
  const liveRow = allRows.find((row) => row.rowKey === "item:live-item");
  const controlRow = allRows.find((row) => row.rowKey === "control:model");
  const commandRow = allRows.find((row) => row.rowKey === "command:sync-command");
  const interestedRow = interest ? allRows.find((row) => row.itemId === interest.itemId) : undefined;
  const latestPayloadRef = interest ? payloadRef(interestedRow, interest.field) : null;
  const latestGenerationId = interest ? payloadGeneration(interestedRow, interest.field) : null;
  const oldestAnchor = allRows.reduce<bigint | null>(
    (oldest, row) => (oldest === null || row.anchor < oldest ? row.anchor : oldest),
    null
  );

  useEffect(() => {
    if (!pinnedPayloadRef || loadedPinnedRef.current) return;
    loadedPinnedRef.current = true;
    const controller = new AbortController();
    const token = localStorage.getItem("spike-token") ?? "";
    void fetch(`/api/payloads/${encodeURIComponent(conversationId)}/${encodeURIComponent(pinnedPayloadRef)}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Pinned payload manifest request failed with ${response.status}`);
        return payloadManifestSchema.parse(await response.json());
      })
      .then((manifest) => {
        if (controller.signal.aborted || manifest.conversationId !== conversationId) return;
        setInterest({
          itemId: manifest.itemId,
          field: manifest.fieldName,
          payloadRef: manifest.payloadRef,
          generationId: manifest.generationId,
          shapeRef: manifest.payloadRef,
          followLatest: false,
          revision: manifest.revision,
          chunkCount: manifest.chunkCount,
          contentBytes: manifest.contentBytes,
          epoch: ++nextPayloadEpoch.current,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        window.__syncEvidence.pageErrors.push(String(error));
      });
    return () => controller.abort();
  }, [conversationId, pinnedPayloadRef]);

  useEffect(() => {
    window.__syncEvidence.rowKeys = tailRows.map((row) => row.rowKey);
    window.__syncEvidence.exactBigint = tailRows.some((row) => row.anchor === BigInt("9007199254740993"));
    for (const row of tailRows) {
      const revisions = (window.__syncEvidence.revisions[row.rowKey] ??= []);
      const latest = row.revision.toString();
      if (revisions.at(-1) !== latest) revisions.push(latest);
    }
    const keys = new Set(tailRows.map((row) => row.rowKey));
    if (keys.has("item:tool-row") && keys.has("control:model") && keys.has("command:sync-command")) {
      window.__syncEvidence.atomicStates.push([
        toolRow?.argumentsRevision.toString() ?? "missing",
        controlRow?.model ?? "missing",
        commandRow?.status ?? "missing",
      ]);
    }
  }, [tailRows, toolRow, controlRow, commandRow]);

  useLayoutEffect(() => {
    const pending = pendingScroll.current;
    const scroller = viewport.current;
    if (!pending || !scroller || allRows.length <= pending.rowCount) return;
    const anchor = [...scroller.querySelectorAll<HTMLElement>("[data-row-key]")].find(
      (element) => element.dataset.rowKey === pending.key
    );
    if (!anchor) return;
    const preCorrectionTop = anchor.getBoundingClientRect().top;
    const correctionPx = preCorrectionTop - pending.top;
    scroller.scrollTop += correctionPx;
    const afterTop = anchor.getBoundingClientRect().top;
    const delta = afterTop - pending.top;
    window.__syncEvidence.scroll.push({
      key: pending.key,
      beforeTop: pending.top,
      preCorrectionTop,
      afterTop,
      correctionPx,
      delta,
    });
    pendingScroll.current = null;
  }, [allRows]);

  const onPayloadReady = useCallback((epoch: number, ref: string) => {
    setPayloadReady((current) =>
      current?.epoch === epoch && current.payloadRef === ref ? current : { epoch, payloadRef: ref }
    );
  }, []);

  useEffect(() => {
    if (
      !interest ||
      !interest.followLatest ||
      !interestedRow ||
      latestPayloadRef === null ||
      latestPayloadRef === interest.payloadRef
    ) {
      return;
    }
    const previousValueReady =
      interest.payloadRef === null ||
      (payloadReady?.epoch === interest.epoch && payloadReady.payloadRef === interest.payloadRef);
    if (!previousValueReady) return;
    setInterest((current) => {
      if (!current || current.epoch !== interest.epoch) return current;
      const generationId = latestGenerationId;
      return {
        ...current,
        payloadRef: latestPayloadRef,
        generationId,
        shapeRef: generationId === current.generationId ? current.shapeRef : latestPayloadRef,
        revision: payloadRevision(interestedRow, interest.field),
        chunkCount: payloadChunkCount(interestedRow, interest.field),
        contentBytes: payloadContentBytes(interestedRow, interest.field),
      };
    });
    setPayloadReady(null);
  }, [interest, interestedRow, latestGenerationId, latestPayloadRef, payloadReady]);

  function loadOlder() {
    if (oldestAnchor === null || !viewport.current) return;
    const scroller = viewport.current;
    const anchor = [...scroller.querySelectorAll<HTMLElement>("[data-row-key]")].find((element) => {
      const rect = element.getBoundingClientRect();
      const box = scroller.getBoundingClientRect();
      return rect.bottom > box.top && rect.top < box.bottom;
    });
    if (anchor?.dataset.rowKey) {
      pendingScroll.current = {
        key: anchor.dataset.rowKey,
        top: anchor.getBoundingClientRect().top,
        rowCount: allRows.length,
      };
    }
    setHistoryCursors((current) => [...current, oldestAnchor]);
  }

  function openPayload(itemId: string, field: PayloadField, followLatest = true) {
    const row = allRows.find((candidate) => candidate.itemId === itemId);
    const ref = payloadRef(row, field);
    setPayloadReady(null);
    setInterest({
      itemId,
      field,
      payloadRef: ref,
      generationId: payloadGeneration(row, field),
      shapeRef: ref,
      followLatest,
      revision: payloadRevision(row, field),
      chunkCount: payloadChunkCount(row, field),
      contentBytes: payloadContentBytes(row, field),
      epoch: ++nextPayloadEpoch.current,
    });
  }

  function closePayload() {
    nextPayloadEpoch.current += 1;
    setInterest(null);
    setPayloadReady(null);
  }

  return (
    <main data-testid="sync-app" data-status={tail.isError ? "error" : tail.isLoading ? "loading" : "ready"}>
      <h1>Thread {conversationId}</h1>
      <output data-testid="tail-count">{tailRows.length}</output>
      <output data-testid="history-count">
        {Object.values(historyByPage).reduce((total, rows) => total + rows.length, 0)}
      </output>
      <output data-testid="exact-bigint">
        {tailRows.some((row) => row.anchor === BigInt("9007199254740993")) ? "exact" : "waiting"}
      </output>
      {tail.isError ? <pre data-testid="sync-error">{String(tail.status)}</pre> : null}
      <div className="toolbar">
        <button type="button" onClick={loadOlder} aria-label="Load older">
          Load older
        </button>
        <button type="button" onClick={() => openPayload(payloadItem, "text")} aria-label="Open text">
          Open text
        </button>
        <button type="button" onClick={() => openPayload(payloadItem, "text", false)} aria-label="Pin text revision">
          Pin text revision
        </button>
        <button type="button" onClick={() => openPayload("tool-row", "arguments")} aria-label="Open arguments">
          Open arguments
        </button>
        <button type="button" onClick={() => openPayload("tool-row", "output")} aria-label="Open output">
          Open output
        </button>
        <button type="button" onClick={() => openPayload("older-tool-row", "output")} aria-label="Open older output">
          Open older output
        </button>
        <button type="button" onClick={() => openPayload("reasoning-row", "reasoning")} aria-label="Open reasoning">
          Open reasoning
        </button>
        <button type="button" onClick={closePayload} aria-label="Close payload">
          Close payload
        </button>
      </div>
      <div className="atomic" data-testid="atomic-state">
        {toolRow?.argumentsRevision.toString() ?? "missing"}|{controlRow?.model ?? "missing"}|
        {commandRow?.status ?? "missing"}
      </div>
      {interest === null ? (
        <output data-testid="payload-body" data-state="closed" />
      ) : isActivePayloadInterest(interest) ? (
        <PayloadPanel
          conversationId={conversationId}
          selection={interest}
          latestRef={latestPayloadRef}
          onReady={onPayloadReady}
          key={interest.epoch}
        />
      ) : (
        <output
          data-testid="payload-body"
          data-state={interest.payloadRef === null ? "absent" : "error"}
          data-item-id={interest.itemId}
          data-field={interest.field}
          data-epoch={interest.epoch}
        >
          {interest.payloadRef === null ? "Body absent" : "Payload reference is incomplete"}
        </output>
      )}
      <div className="viewport" ref={viewport} data-testid="viewport">
        {allRows.map((row) => (
          <div
            className="row"
            data-testid="row"
            data-row-key={row.rowKey}
            data-kind={row.entityKind}
            data-anchor={row.anchor.toString()}
            data-revision={row.revision.toString()}
            data-status={row.status ?? ""}
            data-model={row.model ?? ""}
            data-text-revision={row.textRevision.toString()}
            data-arguments-revision={row.argumentsRevision.toString()}
            data-output-revision={row.outputRevision.toString()}
            data-reasoning-revision={row.reasoningRevision.toString()}
            data-text-payload-ref={row.textPayloadRef ?? ""}
            data-text-generation-id={row.textGenerationId ?? ""}
            data-text-chunk-count={row.textChunkCount.toString()}
            data-arguments-payload-ref={row.argumentsPayloadRef ?? ""}
            data-arguments-generation-id={row.argumentsGenerationId ?? ""}
            data-output-payload-ref={row.outputPayloadRef ?? ""}
            data-output-generation-id={row.outputGenerationId ?? ""}
            key={row.rowKey}
          >
            {row.rowKey} · {row.status ?? row.model ?? row.entityKind}
          </div>
        ))}
      </div>
      {historyCursors.map((before) => (
        <HistoryPage
          collection={collection}
          before={before}
          pageId={before.toString()}
          onRows={receiveHistory}
          key={before.toString()}
        />
      ))}
    </main>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root");
createRoot(root).render(<App />);
