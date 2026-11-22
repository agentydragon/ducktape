import type { Dispatch } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import type { LocalCommands } from "./local_commands";

/** Only returned, protected members of the Session's paged command history belong in this set. */
export type ReadRetainedDispatches = (
  sessionId: string,
  ids: readonly string[],
  signal: AbortSignal
) => Promise<readonly Dispatch[]>;

/** Run while the browser offers paginated, payload-readable receipt history for this Session.
 * This observer only reads the server. Submission and same-ID recovery have their own owner. */
export function watchCommandOwnership(store: LocalCommands, readDispatches: ReadRetainedDispatches): () => void {
  const stop = new AbortController();
  const checked = new Map<string, boolean>();
  let running = false;

  async function check(): Promise<void> {
    if (running || stop.signal.aborted || !navigator.onLine) return;
    running = true;
    try {
      while (!stop.signal.aborted && navigator.onLine) {
        const candidates = store.ownershipCandidates();
        for (const id of checked.keys()) if (!candidates.has(id)) checked.delete(id);
        const batch: string[] = [];
        for (const [id, hasReceipt] of candidates) {
          if (checked.get(id) === hasReceipt) continue;
          checked.set(id, hasReceipt);
          batch.push(id);
          if (batch.length === 16) break;
        }
        if (batch.length === 0) return;
        let members: readonly Dispatch[];
        try {
          members = await readDispatches(store.threadId, batch, stop.signal);
        } catch {
          // A mixed rollout or outage leaves the authored copy intact. Retry on reconnect/focus.
          continue;
        }
        const requested = new Set(batch);
        for (const member of members) {
          if (stop.signal.aborted) return;
          if (!requested.delete(member.commandId) || member.sessionId !== store.threadId) continue;
          store.transferToDispatcher(member);
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
