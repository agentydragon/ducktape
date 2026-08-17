import { useEffect, useState } from "react";

import { fetchOperator } from "./client";

// How long before the deadline the shell starts saying so. The operator session has a hard,
// non-sliding one-hour lifetime, so expiry always arrives — as a warning here, or as a tab that
// navigates itself to Authentik mid-task.
export const SESSION_WARNING_LEAD_MS = 5 * 60_000;
const TICK_MS = 30_000;

/** The absolute deadline of the current operator session, or `null` until it is known.
 *
 * Read once at mount: the deadline is fixed at login and never extends, so there is nothing to
 * poll for. The tick only advances the clock the shell compares against. */
export function useOperatorSessionDeadline(): Date | null {
  const [expiresAt, setExpiresAt] = useState<Date | null>(null);

  useEffect(() => {
    let alive = true;
    void fetchOperator().then(
      (operator) => {
        if (alive) setExpiresAt(new Date(operator.expires_at));
      },
      (error: unknown) => {
        // A 401 has already sent the browser to the login flow; anything else costs only the
        // warning, so the shell degrades to the pre-existing behavior rather than failing.
        console.warn("Unable to read the operator session deadline", error);
      }
    );
    return () => {
      alive = false;
    };
  }, []);

  return expiresAt;
}

/** Whether `expiresAt` is close enough to warn about, re-evaluated on a coarse tick. */
export function useSessionExpiringSoon(expiresAt: Date | null): boolean {
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (expiresAt === null) return;
    const tick = window.setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => window.clearInterval(tick);
  }, [expiresAt]);

  return expiresAt !== null && expiresAt.getTime() - nowMs <= SESSION_WARNING_LEAD_MS;
}
