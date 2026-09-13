import { useCallback, useEffect, useState, type JSX } from "react";

import { Dashboard } from "./dashboard";
import type { QuotasView } from "./quotas";

/** The service keeps its snapshot for 120s, so polling faster only re-reads the same one. */
const POLL_INTERVAL_MS = 120_000;
/** Reset countdowns are the only thing that moves between polls; tick them like a clock. */
const TICK_MS = 1_000;

export function App(): JSX.Element {
  const [quotas, setQuotas] = useState<QuotasView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [now, setNow] = useState(() => Date.now());

  const refresh = useCallback(async (): Promise<void> => {
    setRefreshing(true);
    try {
      const response = await fetch("/v1/quotas", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) {
        throw new Error(
          response.status === 401
            ? "Your OAuth session has expired. Refresh to sign in again."
            : `Quota API returned ${response.status}.`
        );
      }
      setQuotas((await response.json()) as QuotasView);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load quota data.");
    } finally {
      setRefreshing(false);
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

  return <Dashboard quotas={quotas} now={now} error={error} refreshing={refreshing} onRefresh={() => void refresh()} />;
}
