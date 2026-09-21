import { snakeCamelMapper } from "@electric-sql/client";
import { electricCollectionOptions } from "@tanstack/electric-db-collection";
import { createCollection, eq, lt, useLiveQuery } from "@tanstack/react-db";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { z } from "zod";

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
  status: z.string().nullable(),
  model: z.string().nullable(),
  commandId: z.string().nullable(),
});

type ViewRow = z.output<typeof viewRowSchema>;
type PayloadField = "text" | "arguments" | "output" | "reasoning";
type PayloadInterest = { itemId: string; field: PayloadField } | null;
type PayloadPart = { sourceCursor: string; operation: "append" | "replace"; content: string };

declare global {
  interface Window {
    __syncEvidence: {
      atomicStates: string[][];
      revisions: Record<string, string[]>;
      rowKeys: string[];
      exactBigint: boolean;
      scroll: Array<{ key: string; beforeTop: number; afterTop: number; delta: number }>;
      pageErrors: string[];
    };
  }
}

window.__syncEvidence = {
  atomicStates: [],
  revisions: {},
  rowKeys: [],
  exactBigint: false,
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

function rowVersion(row: ViewRow, field: PayloadField): bigint {
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
  const collection = useMemo(() => makeRows(conversationId), [conversationId]);
  const tail = useLiveQuery(
    (q) => q.from({ row: collection }).orderBy(({ row }) => row.anchor, "desc").limit(30),
    [collection]
  );
  const tailRows = tail.data ?? [];
  const [historyCursors, setHistoryCursors] = useState<bigint[]>([]);
  const [historyByPage, setHistoryByPage] = useState<Record<string, ViewRow[]>>({});
  const [interest, setInterest] = useState<PayloadInterest>(null);
  const [payloadCursor, setPayloadCursor] = useState("0");
  const [payloadText, setPayloadText] = useState("");
  const viewport = useRef<HTMLDivElement>(null);
  const pendingScroll = useRef<{ key: string; top: number; rowCount: number } | null>(null);
  const receiveHistory = useCallback((pageId: string, rows: ViewRow[]) => {
    setHistoryByPage((current) => (current[pageId] === rows ? current : { ...current, [pageId]: rows }));
  }, []);
  const allRows = useMemo(() => {
    const unique = new Map<string, ViewRow>();
    for (const row of tailRows) unique.set(row.rowKey, row);
    for (const rows of Object.values(historyByPage)) for (const row of rows) unique.set(row.rowKey, row);
    return [...unique.values()].sort((left, right) => (left.anchor < right.anchor ? -1 : left.anchor > right.anchor ? 1 : 0));
  }, [tailRows, historyByPage]);
  const toolRow = allRows.find((row) => row.rowKey === "item:tool-row");
  const liveRow = allRows.find((row) => row.rowKey === "item:live-item");
  const controlRow = allRows.find((row) => row.rowKey === "control:model");
  const commandRow = allRows.find((row) => row.rowKey === "command:sync-command");
  const interestedRow = interest ? allRows.find((row) => row.itemId === interest.itemId) : undefined;
  const interestedVersion = interest && interestedRow ? rowVersion(interestedRow, interest.field) : 0n;
  const oldestAnchor = allRows.reduce<bigint | null>((oldest, row) => (oldest === null || row.anchor < oldest ? row.anchor : oldest), null);

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
    const afterTop = anchor.getBoundingClientRect().top;
    const delta = afterTop - pending.top;
    scroller.scrollTop += delta;
    window.__syncEvidence.scroll.push({ key: pending.key, beforeTop: pending.top, afterTop, delta });
    pendingScroll.current = null;
  }, [allRows]);

  useEffect(() => {
    if (!interest || !interestedRow || payloadCursor === interestedVersion.toString()) return;
    const controller = new AbortController();
    const token = localStorage.getItem("spike-token") ?? "";
    const url = `/api/payloads/${encodeURIComponent(conversationId)}/${encodeURIComponent(interest.itemId)}/${interest.field}?after=${encodeURIComponent(payloadCursor)}`;
    void fetch(url, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Payload request failed with ${response.status}`);
        return (await response.json()) as { rows: PayloadPart[] };
      })
      .then(({ rows }) => {
        let content = payloadText;
        let through = payloadCursor;
        for (const part of rows) {
          content = part.operation === "replace" ? part.content : content + part.content;
          through = part.sourceCursor;
        }
        setPayloadText(content);
        setPayloadCursor(through);
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          window.__syncEvidence.pageErrors.push(String(error));
        }
      });
    return () => controller.abort();
  }, [conversationId, interest, interestedRow, interestedVersion, payloadCursor, payloadText]);

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

  function openPayload(itemId: string, field: PayloadField) {
    setPayloadText("");
    setPayloadCursor("0");
    setInterest({ itemId, field });
  }

  function closePayload() {
    setInterest(null);
    setPayloadText("");
    setPayloadCursor("0");
  }

  return (
    <main data-testid="sync-app" data-status={tail.isError ? "error" : tail.isLoading ? "loading" : "ready"}>
      <h1>Thread {conversationId}</h1>
      <output data-testid="tail-count">{tailRows.length}</output>
      <output data-testid="history-count">{Object.values(historyByPage).reduce((total, rows) => total + rows.length, 0)}</output>
      <output data-testid="exact-bigint">
        {tailRows.some((row) => row.anchor === BigInt("9007199254740993")) ? "exact" : "waiting"}
      </output>
      {tail.isError ? <pre data-testid="sync-error">{String(tail.status)}</pre> : null}
      <div className="toolbar">
        <button type="button" onClick={loadOlder} aria-label="Load older">
          Load older
        </button>
        <button type="button" onClick={() => openPayload("live-item", "text")} aria-label="Open text">
          Open text
        </button>
        <button type="button" onClick={() => openPayload("tool-row", "arguments")} aria-label="Open arguments">
          Open arguments
        </button>
        <button type="button" onClick={() => openPayload("tool-row", "output")} aria-label="Open output">
          Open output
        </button>
        <button type="button" onClick={() => openPayload("reasoning-row", "reasoning")} aria-label="Open reasoning">
          Open reasoning
        </button>
        <button type="button" onClick={closePayload} aria-label="Close payload">
          Close payload
        </button>
      </div>
      <div className="atomic" data-testid="atomic-state">
        {toolRow?.argumentsRevision.toString() ?? "missing"}|{controlRow?.model ?? "missing"}|{commandRow?.status ?? "missing"}
      </div>
      <output data-testid="payload-body">{payloadText}</output>
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
