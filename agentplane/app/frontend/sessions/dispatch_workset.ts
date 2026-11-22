import { create, fromJson, type JsonValue } from "@bufbuild/protobuf";
import {
  DeliveryState,
  DispatchWorksetItemSchema,
  WatchDispatchesResponseSchema,
  type Dispatch,
  type DispatchWorkset,
  type DispatchWorksetItem,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError, HttpError } from "../transport/client";
import { sessionDispatcherClient } from "../transport/session_dispatcher_client";
import { WatchStreamError } from "../transport/watch_stream";
import { batchGetDispatches } from "./client";

const LEASE_RENEWAL_GRACE_MS = 3_000;
const INITIAL_WORKSET_TIMEOUT_MS = 60_000;
const RECEIPT_LOOKUP_TIMEOUT_MS = 15_000;

export interface WorksetView {
  snapshot: DispatchWorkset | null;
  stale: boolean;
  coverageLost: boolean;
}

export const EMPTY_WORKSET: WorksetView = { snapshot: null, stale: false, coverageLost: false };

export function worksetItemFromReceipt(row: Dispatch): DispatchWorksetItem {
  return create(DispatchWorksetItemSchema, {
    commandId: row.commandId,
    operation: row.operation,
    state: row.state,
    reason: row.reason,
    updatedAt: row.updatedAt,
    receiptVersion: row.receiptVersion,
    journalAdmitted: row.admission !== undefined,
  });
}

/** Read the exact current or legacy receipt, with an explicit bound on diagnostic lookups. */
export async function readSelectedDispatchReceipts(
  sessionId: string,
  commandIds: readonly string[],
  signal: AbortSignal
): Promise<Dispatch[]> {
  const results = await batchGetDispatches(
    sessionId,
    commandIds,
    AbortSignal.any([signal, AbortSignal.timeout(RECEIPT_LOOKUP_TIMEOUT_MS)])
  );
  return results.flatMap(({ result }) =>
    result.case === "dispatch" || result.case === "legacyDeletionReceipt" ? [result.value] : []
  );
}

function decodeFrame(result: JsonValue) {
  const frame = fromJson(WatchDispatchesResponseSchema, result, { ignoreUnknownFields: true });
  if (!frame.resumeToken) throw new Error("Dispatcher omitted the watch checkpoint");
  return frame;
}

function checkWorkset(snapshot: DispatchWorkset, sessionId: string): void {
  if (snapshot.sessionId !== sessionId) throw new Error("Dispatcher returned another Session's workset");
  const ids = new Set<string>();
  const groups = [
    {
      rows: snapshot.active,
      limit: 1024,
      states: [DeliveryState.QUEUED_IN_DISPATCHER, DeliveryState.DELIVERING_TO_RUNNER],
    },
    { rows: snapshot.recentIssues, limit: 20, states: [DeliveryState.FAILED, DeliveryState.UNKNOWN] },
    {
      rows: snapshot.recentAdmittedOrCancelled,
      limit: 20,
      states: [DeliveryState.RUNNER_ADMITTED, DeliveryState.CANCELLED_IN_DISPATCHER],
    },
  ];
  for (const { rows, limit, states } of groups) {
    if (rows.length > limit) throw new Error("Dispatcher workset exceeded its limit");
    for (const item of rows) {
      if (!item.commandId || ids.has(item.commandId) || !states.includes(item.state)) {
        throw new Error("Dispatcher returned an invalid workset item");
      }
      ids.add(item.commandId);
    }
  }
}

export function watchDispatchWorkset(sessionId: string, publish: (view: WorksetView) => void): () => void {
  let token = "";
  let view = EMPTY_WORKSET;
  let renewal: number | undefined;
  const update = (next: WorksetView) => {
    view = next;
    publish(view);
  };
  const stop = sessionDispatcherClient.watchDispatches(
    () => new URLSearchParams({ worksetSessionId: sessionId, ...(token && { resumeToken: token }) }),
    {
      initialFrameTimeoutMs: INITIAL_WORKSET_TIMEOUT_MS,
      onOpen: () => undefined,
      onFrame: (result) => {
        const frame = decodeFrame(result);
        if (frame.frame.case !== "workset") throw new Error("Dispatcher omitted the Session workset");
        checkWorkset(frame.frame.value, sessionId);
        token = frame.resumeToken;
        window.clearTimeout(renewal);
        update({ ...view, snapshot: frame.frame.value, stale: false });
      },
      onConnection: (connection) => {
        if (connection.phase !== "reconnecting") return;
        window.clearTimeout(renewal);
        if (connection.lastError !== null) {
          update({ ...view, stale: true });
        } else {
          renewal = window.setTimeout(() => update({ ...view, stale: true }), LEASE_RENEWAL_GRACE_MS);
        }
      },
      onError: (reason) => {
        if (!(reason instanceof WatchStreamError || reason instanceof HttpError) || reason.code !== 11) return;
        token = "";
        update({ ...view, stale: true, coverageLost: true });
      },
    }
  );
  return () => {
    stop();
    window.clearTimeout(renewal);
  };
}

export type ReasonLookup = { reason: string } | { error: string };

export function readFullDispatchReason(
  sessionId: string,
  item: DispatchWorksetItem,
  publish: (result: ReasonLookup) => void
): () => void {
  const controller = new AbortController();
  void readSelectedDispatchReceipts(sessionId, [item.commandId], controller.signal).then(
    ([row]) => {
      if (controller.signal.aborted) return;
      const sameUpdate =
        item.updatedAt?.seconds === row?.updatedAt?.seconds && item.updatedAt?.nanos === row?.updatedAt?.nanos;
      const sameVersion = item.receiptVersion === undefined || item.receiptVersion === row?.receiptVersion;
      if (
        row &&
        sameVersion &&
        row.state === item.state &&
        row.operation === item.operation &&
        sameUpdate &&
        row.reason?.startsWith(item.reason ?? "")
      ) {
        publish({ reason: row.reason });
      } else {
        publish({ error: "Dispatcher could not return the original reason." });
      }
    },
    (error: unknown) => {
      if (!controller.signal.aborted) publish({ error: displayableError(error) });
    }
  );
  return () => controller.abort();
}
