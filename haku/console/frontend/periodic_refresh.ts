import { useEffect, useRef } from "react";

export const POLL_REFRESH_INTERVAL_MS = 30_000;

/** Refresh once on mount and periodically while the view is open. */
export function usePeriodicRefresh(refresh: () => void): void {
  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  useEffect(() => {
    refreshRef.current();
    const interval = window.setInterval(() => refreshRef.current(), POLL_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, []);
}
