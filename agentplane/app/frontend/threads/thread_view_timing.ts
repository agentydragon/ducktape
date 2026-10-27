/**
 * What `VirtualizedHistory` in `virtualized_history.tsx` marks for inspection, for tests and for chasing a reader-position
 * bug: User Timing marks for its scroll and layout decisions, and whether its layout has come
 * to rest. The Playwright harness installs the test-only mark collector before the app loads.
 */

/** Why the history started or stopped following the tail. */
export type FollowReason =
  | "jump-to-latest"
  | "returned-to-previous-bottom"
  | "scrolled-to-bottom"
  | "scrolled-up"
  | "wheel-up"
  | "key-up"
  | "touch-up"
  | "disclosure-click";

/** Mirrored by the typed events in agentplane/app/testing/thread_view_marks.py. */
export type ThreadViewEvent =
  | { kind: "follow"; following: boolean; reason: FollowReason }
  | {
      kind: "scroll";
      scrollTop: number;
      previousTop: number;
      scrollHeight: number;
      clientHeight: number;
      followed: boolean;
    }
  | {
      kind: "input";
      source: "wheel" | "key" | "touch" | "pointer";
      direction: "up" | "down" | "unknown";
      scrollTop: number;
    }
  | { kind: "virtual-size"; before: number; after: number; sync: boolean; following: boolean }
  | { kind: "scrollend"; restoring: boolean; capturing: boolean }
  /** The history's content or tail changed size. `pinned`: it was followed to the bottom. */
  | { kind: "resize"; scrollTop: number; scrollHeight: number; pinned: boolean }
  | { kind: "click"; scrollTop: number; scrollHeight: number; clientHeight: number }
  | { kind: "anchor"; key: string; offset: number }
  | { kind: "restore"; key: string; correction: number | null }
  | { kind: "prepend"; added: number }
  | { kind: "marker" }
  | { kind: "load-older" }
  | { kind: "older-state"; loading: boolean; available: boolean; rowCount: number }
  /** A row's height was read. `estimate` is what the virtualizer laid it out with before: a
   * remembered reading of an earlier visit if `remembered`, else a flat guess. */
  | { kind: "measure"; key: string; estimate: number; measured: number; first: boolean; remembered: boolean }
  | { kind: "settled"; settled: boolean };

declare global {
  interface Window {
    /** Set by the Playwright init script before the app loads, not by production code. */
    __agentplaneThreadViewMarksEnabled?: boolean;
  }
}

const MAX_CAPTURE_MARKS = 20_000;
let capture: { recorded: number; dropped: number } | null = null;

/** Enabled by the on-device recorder. Tests use their own flag, set before app startup. */
export function startThreadViewMarks(): void {
  capture = { recorded: 0, dropped: 0 };
}

export function stopThreadViewMarks(): number {
  const dropped = capture?.dropped ?? 0;
  capture = null;
  return dropped;
}

export function markThreadViewEvent(event: ThreadViewEvent): void {
  if (!capture && !window.__agentplaneThreadViewMarksEnabled) return;
  if (capture && capture.recorded++ >= MAX_CAPTURE_MARKS) {
    capture.dropped++;
    return;
  }
  performance.mark(`agentplane:thread-view:${event.kind}`, { detail: event });
}

/** A layout counts as at rest once this many frames pass without it changing. */
const QUIET_FRAMES = 5;

/**
 * Whether a layout has come to rest. `changed()` says it moved; it is unsettled until it has
 * stayed put for `QUIET_FRAMES` frames, and while `busy()` says something that will move it is still
 * under way: a correction, or content a row is waiting for.
 */
export class LayoutSettle {
  readonly #busy: () => boolean;
  readonly #onChange: (settled: boolean) => void;
  #settled = false;
  #frame: number | null = null;

  constructor(busy: () => boolean, onChange: (settled: boolean) => void) {
    this.#busy = busy;
    this.#onChange = onChange;
    this.#wait(0);
  }

  changed(): void {
    if (this.#settled) {
      this.#settled = false;
      this.#onChange(false);
    }
    if (this.#frame !== null) cancelAnimationFrame(this.#frame);
    this.#wait(0);
  }

  dispose(): void {
    if (this.#frame !== null) cancelAnimationFrame(this.#frame);
    this.#frame = null;
  }

  #wait(quiet: number): void {
    this.#frame = requestAnimationFrame(() => {
      if (this.#busy()) return this.#wait(0);
      if (quiet + 1 < QUIET_FRAMES) return this.#wait(quiet + 1);
      this.#frame = null;
      this.#settled = true;
      this.#onChange(true);
    });
  }
}
