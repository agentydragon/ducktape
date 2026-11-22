import type { BatchGetDispatchesResponse_Result } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { MAX_DISPATCH_BATCH } from "./client";
import type { ReadDispatchReceipts } from "./dispatch_workset";
import type { LocalCommands } from "./local_commands";

/** Run while the browser offers paginated, payload-readable receipt history for this Session.
 * This observer only reads the server. Submission and same-ID recovery have their own owner. */
export function watchCommandOwnership(
  store: LocalCommands,
  readReceipts: ReadDispatchReceipts,
  connection: AbortSignal
): () => void {
  const stop = new AbortController();
  const signal = AbortSignal.any([stop.signal, connection]);
  const checked = new Map<string, boolean>();
  let running = false;

  async function check(): Promise<void> {
    if (running || signal.aborted || !navigator.onLine) return;
    running = true;
    try {
      while (!signal.aborted && navigator.onLine) {
        const candidates = store.ownershipCandidates();
        for (const id of checked.keys()) if (!candidates.has(id)) checked.delete(id);
        const batch: string[] = [];
        for (const [id, hasReceipt] of candidates) {
          if (checked.get(id) === hasReceipt) continue;
          checked.set(id, hasReceipt);
          batch.push(id);
          if (batch.length === MAX_DISPATCH_BATCH) break;
        }
        if (batch.length === 0) return;
        let results: readonly BatchGetDispatchesResponse_Result[];
        try {
          results = await readReceipts(batch, signal);
        } catch {
          // A mixed rollout or outage leaves the authored copy intact. Retry on reconnect/focus.
          continue;
        }
        const requested = new Set(batch);
        for (const result of results) {
          if (signal.aborted) return;
          if (!requested.delete(result.commandId)) continue;
          store.transferToDispatcher(result);
        }
      }
    } catch {
      // Storage may be inaccessible. Nothing from the server authorizes dropping unreadable bytes.
    } finally {
      running = false;
    }
  }

  // Coalesce synchronous storage notifications (including our own) into the active read loop.
  const changed = () => {
    void check();
  };
  const reconnect = () => {
    checked.clear();
    changed();
  };
  const unsubscribe = store.subscribe(changed);
  window.addEventListener("online", reconnect);
  window.addEventListener("focus", reconnect);
  changed();
  return () => {
    stop.abort();
    unsubscribe();
    window.removeEventListener("online", reconnect);
    window.removeEventListener("focus", reconnect);
  };
}
