import { useCallback, useEffect, useState, type JSX } from "react";

import { ApiError, getStatus, syncNow, type SyncStatus } from "./api";
import { Overview } from "./overview";
import { Pairing } from "./pairing";
import { SessionViewer } from "./viewer";

/** A cycle moves the counts, and pairing or "Sync now" should show up without a reload. */
const POLL_INTERVAL_MS = 5_000;
/** The relative times are the only thing that moves between polls; tick them like a clock. */
const TICK_MS = 1_000;

export function App(): JSX.Element {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setStatus(await getStatus());
      setError(null);
    } catch (reason) {
      setError(reason instanceof ApiError || reason instanceof Error ? reason.message : "Could not load the status.");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const poll = globalThis.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => globalThis.clearInterval(poll);
  }, [refresh]);

  useEffect(() => {
    const tick = globalThis.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => globalThis.clearInterval(tick);
  }, []);

  const requestSync = useCallback(async (): Promise<void> => {
    try {
      await syncNow();
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start a sync.");
    }
  }, [refresh]);

  return (
    <main>
      <h1>Claude session sync</h1>
      {error !== null && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {status === null ? (
        error === null && <p>Loading…</p>
      ) : (
        <>
          <Overview status={status} now={now} onSyncNow={() => void requestSync()} />
          <Pairing paired={status.credential !== null} onPaired={setStatus} />
          <SessionViewer />
        </>
      )}
    </main>
  );
}
