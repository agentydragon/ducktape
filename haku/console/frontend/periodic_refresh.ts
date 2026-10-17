import { useEffect, useEffectEvent } from "react";

export const POLL_REFRESH_INTERVAL_MS = 30_000;

/** Refresh once on mount and periodically while the view is open. */
export function usePeriodicRefresh(refresh: () => void): void {
  const refreshOnTick = useEffectEvent(refresh);

  useEffect(() => {
    refreshOnTick();
    const interval = window.setInterval(() => refreshOnTick(), POLL_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, []);
}
