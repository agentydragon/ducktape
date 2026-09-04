/**
 * The deadline every wait in a visual test shares.
 *
 * Bazel hands the test its own timeout in `TEST_TIMEOUT` (seconds), so a bound derived from it
 * scales with the size the target declares. A literal does not: five seconds for the SPA's first
 * mount was comfortable on an idle machine and expired on a loaded RBE worker while the bundle
 * was still legitimately loading (debug/2026_08_rbe_small_test_timeouts.md), which reports an
 * arbitrary elapsed time rather than the page failing to mount.
 *
 * One deadline for the whole process, not a fresh budget per wait: whichever wait is blocking
 * when it arrives is the one that fails, with its own message naming what never happened, and the
 * waits before it cannot push that failure past the point where Bazel kills the target — which
 * says only that it timed out. `RESERVE_MS` keeps the margin for the failure to print.
 *
 * Outside Bazel (`bazel run` of a scenario, a bare `node`) nothing declares a timeout and this
 * returns undefined, leaving Puppeteer's own default in place.
 */

// The browser still has to close and the stack to print after a wait fails, which took ~6s of
// this on an RBE worker whose page never mounted.
const RESERVE_MS = 20_000;

// Process start, near enough: this module is imported before any waiting starts.
const startedAtMs = Date.now();

/** How long a wait may block, in ms — or undefined when no test timeout is declared. */
export function remainingWaitMs() {
  const declared = process.env.TEST_TIMEOUT;
  if (declared === undefined) return undefined;
  const seconds = Number(declared);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    throw new Error(`TEST_TIMEOUT is not a positive number of seconds: ${JSON.stringify(declared)}`);
  }
  // Past the deadline a wait still gets a moment, so it fails naming its subject rather than
  // throwing a bare RangeError on a negative timeout.
  return Math.max(1_000, startedAtMs + seconds * 1_000 - RESERVE_MS - Date.now());
}
