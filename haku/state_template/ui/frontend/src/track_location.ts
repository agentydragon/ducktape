import { watchGeolocation, type GeolocationResult } from "./bridge.ts";
import { recordLocation } from "./client.ts";
import { logger } from "./log.ts";

const log = logger("location");

// Continuously track the operator's location and record each fix to the backend (→ the
// time-series store, once wired; a placeholder for now). Started on app load; call the
// returned stop() to tear the watch down (e.g. on unmount). Reads go through the trusted
// console's standing consent grant: the first fix may pop the shell's consent confirm, and
// the operator can stop tracking any time from the console panel (which makes the shell emit
// a terminal "withdrawn" fix — we log it and stop recording).
//
// Best-effort and self-contained: declines / per-fix errors / backend POST failures are
// logged, never surfaced — the app runs fine without a location.
export function startLocationTracking(): () => void {
  const stop = watchGeolocation((fix: GeolocationResult) => {
    if (!fix.ok || !fix.position) {
      log.info(`location fix skipped: ${fix.reason ?? "no position"}`);
      return;
    }
    void recordLocation(fix.position).catch((e: unknown) => log.warn("recording location fix failed", e));
  });
  return stop;
}
