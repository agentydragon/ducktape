import { useCallback, useEffect, useRef, useState } from "react";

/** Runs `read` with at most one call in flight, collapsing a burst of requests into one catch-up.
 *
 * Live events arrive in bursts — an agent's call is submitted, approved and finished within a
 * second, and a streaming turn invalidates its session every coalescing window — while a view only
 * ever shows the newest state. Overlapping refetches would queue up whole pages of records for
 * answers each of which the next one discards, so a request made while one is running sets a flag
 * instead, and the runner does exactly one more pass afterwards however many arrived.
 *
 * `read` owns its own errors: it is the caller that knows what a failure means for its view, and a
 * throw ends the burst rather than being retried immediately (the live channel, and the shell's
 * bounded periodic resync, bring the next one along).
 *
 * `busy` stays true for the whole burst, not per pass, so a spinner bound to it does not blink
 * between the passes of one catch-up.
 */
export function useCoalescedRefresh(read: () => Promise<void>): { refresh: () => void; busy: boolean } {
  const [busy, setBusy] = useState(false);
  // Held in a ref so a changing callback identity never changes `refresh`'s: it is registered with
  // the live-event hook, which holds the first one it is given.
  const readRef = useRef(read);
  useEffect(() => {
    readRef.current = read;
  });
  const running = useRef(false);
  const catchUp = useRef(false);

  const refresh = useCallback(() => {
    if (running.current) {
      catchUp.current = true;
      return;
    }
    running.current = true;
    setBusy(true);
    void (async () => {
      try {
        do {
          // Cleared before the pass, so an event arriving during it asks for another.
          catchUp.current = false;
          await readRef.current();
        } while (catchUp.current);
      } catch (error) {
        // `read` is supposed to own this; anything arriving here would otherwise be an unhandled
        // rejection, and a view left stale with nothing said about why.
        console.error("A coalesced refresh failed", error);
      } finally {
        catchUp.current = false;
        running.current = false;
        setBusy(false);
      }
    })();
  }, []);

  return { refresh, busy };
}
