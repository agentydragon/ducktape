/**
 * What `VirtualizedHistory` publishes about itself, for tests and for chasing a reader-position
 * bug: a flight recorder of its scroll and layout decisions, and whether its layout has come to
 * rest.
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

/** Mirrored by the typed events in agentplane/app/testing/history_trace.py. */
export type HistoryEvent =
  | { kind: "follow"; following: boolean; reason: FollowReason }
  | { kind: "scroll"; scrollTop: number; scrollHeight: number; followed: boolean }
  | { kind: "scrollend"; restoring: boolean; capturing: boolean }
  /** The history's content or tail changed size. `pinned`: it was followed to the bottom. */
  | { kind: "resize"; scrollTop: number; scrollHeight: number; pinned: boolean }
  | { kind: "click"; scrollTop: number; scrollHeight: number; clientHeight: number }
  | { kind: "anchor"; key: string; offset: number }
  | { kind: "restore"; key: string; correction: number | null }
  | { kind: "prepend"; added: number }
  | { kind: "load-older" }
  /** A row's height was read. `estimate` is what the virtualizer laid it out with before: a
   * remembered reading of an earlier visit if `remembered`, else a flat guess. */
  | { kind: "measure"; key: string; estimate: number; measured: number; first: boolean; remembered: boolean }
  | { kind: "settled"; settled: boolean };

export interface TimedHistoryEvent {
  /** `performance.now()`, in milliseconds. */
  at: number;
  event: HistoryEvent;
}

declare global {
  interface Window {
    /** The recent events of the history on screen, oldest first; absent while none is mounted. */
    agentplaneHistoryTrace?: () => readonly TimedHistoryEvent[];
    /** `measured - estimate`, in pixels, for each row's first reading since the page loaded. */
    agentplaneHistoryEstimateErrors?: () => readonly EstimateError[];
  }
}

/** Long enough for every scroll event of a few seconds of gesture plus a thread opening. */
const CAPACITY = 2000;
const ERROR_CAPACITY = 20_000;

export interface EstimateError {
  error: number;
  remembered: boolean;
}

export class HistoryTrace {
  readonly #events: TimedHistoryEvent[] = [];
  readonly #estimateErrors: EstimateError[] = [];

  record(event: HistoryEvent): void {
    this.#events.push({ at: performance.now(), event });
    if (this.#events.length > CAPACITY) this.#events.shift();
    if (event.kind === "measure" && event.first && this.#estimateErrors.length < ERROR_CAPACITY) {
      this.#estimateErrors.push({ error: event.measured - event.estimate, remembered: event.remembered });
    }
  }

  events(): readonly TimedHistoryEvent[] {
    return this.#events;
  }

  estimateErrors(): readonly EstimateError[] {
    return this.#estimateErrors;
  }
}

/** One for the page: it outlives the history component, so it spans a switch between threads. */
export const historyTrace: HistoryTrace = new HistoryTrace();

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
