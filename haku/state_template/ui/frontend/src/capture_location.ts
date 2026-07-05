import { requestGeolocation } from "./bridge.ts";
import { recordLocation } from "./client.ts";
import { logger } from "./log.ts";

const log = logger("location");

// Ask the trusted console shell for the operator's location and hand it to the backend for
// Haku to use (persistence is a backend TODO — a time-series store, not git). Invoked once
// on app load. The shell owns a standing consent grant ("allow until withdrawn"), so the
// operator is prompted only the first time, then it's silent until they withdraw in the
// console panel.
//
// Best-effort and self-contained: a decline/withdraw is an expected non-event (logged at
// info); a backend POST failure degrades loud (logged at warn with the exception). Either
// way the app runs fine without a location, so nothing is surfaced to the operator.
export async function captureLocation(): Promise<void> {
  const result = await requestGeolocation();
  if (!result.ok || !result.position) {
    log.info(`location not captured: ${result.reason ?? "no position"}`);
    return;
  }
  try {
    await recordLocation(result.position);
  } catch (e) {
    log.warn("recording captured location failed", e);
  }
}
