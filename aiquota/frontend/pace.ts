/**
 * Pace math for the browser dashboard — the TypeScript half of `aiquota/pace.py`.
 *
 * The API serves a snapshot; the reset countdown, and therefore the elapsed fraction every
 * number here derives from, keeps moving between snapshots. So this runs client-side, exactly
 * as it does in the GNOME extension, rather than being served pre-computed and going stale
 * between polls.
 *
 * `aiquota/pace.py` is the canonical statement of the thresholds and the derivation (the
 * reasoning is in `aiquota/gnome/DESIGN.md` § Pace math). `format.test.ts` holds this port to
 * the strings that module produces for the shared scenarios, so the two cannot disagree about
 * a pace, a forecast, or a tint without failing.
 */

export type Tint = "cool" | "ok" | "warn" | "hot" | "unknown" | "stale";

export type Pace = {
  deviation: number;
  projectedAtReset: number | null;
  secondsToExhaust: number | null;
  /** Pace is meaningless in the first and last twentieth of a window; too little has happened. */
  stable: boolean;
};

export type WindowMath = { usedPercent: number; resetSeconds: number; windowSeconds: number };

const STABLE_FRACTION = 0.05;
const EXHAUSTED_PERCENT = 100;
const PACE_COOL_BELOW = -10;
const PACE_WARN_ABOVE = 5;
const PACE_HOT_ABOVE = 15;
const SHORT_WIN_HOT_PERCENT = 85;

const TINT_RANK: Record<Tint, number> = { unknown: 0, stale: 0, ok: 1, cool: 1, warn: 2, hot: 3 };

export function isExhausted(window: WindowMath): boolean {
  return window.usedPercent >= EXHAUSTED_PERCENT;
}

export function elapsedFraction(window: WindowMath): number {
  return clamp01((window.windowSeconds - window.resetSeconds) / window.windowSeconds);
}

export function computePace(window: WindowMath): Pace {
  const elapsedSeconds = window.windowSeconds - window.resetSeconds;
  const elapsed = elapsedSeconds / window.windowSeconds;
  const spending = elapsedSeconds > 0 && window.usedPercent > 0;
  const ratePerSecond = window.usedPercent / elapsedSeconds;
  return {
    deviation: window.usedPercent - elapsed * 100,
    projectedAtReset: spending ? window.usedPercent + ratePerSecond * window.resetSeconds : null,
    secondsToExhaust: spending ? (100 - window.usedPercent) / ratePerSecond : null,
    stable: elapsed > STABLE_FRACTION && elapsed < 1 - STABLE_FRACTION,
  };
}

/**
 * `isShort`: this is not the provider's longest window. A short window at 85% is the
 * immediate-pain case — the next burst gets 429s — whatever its pace says.
 */
export function tintFor(pace: Pace | null, usedPercent: number, { isShort }: { isShort: boolean }): Tint {
  if (isShort && usedPercent >= SHORT_WIN_HOT_PERCENT) return "hot";
  if (pace === null || !pace.stable) {
    if (usedPercent >= 95) return "hot";
    if (usedPercent >= 80) return "warn";
    return "ok";
  }
  if (pace.deviation >= PACE_HOT_ABOVE) return "hot";
  if (pace.deviation >= PACE_WARN_ABOVE) return "warn";
  if (pace.deviation <= PACE_COOL_BELOW) return "cool";
  return "ok";
}

/** The tint a whole provider takes: the most urgent of its windows'. */
export function bindingTint(tints: Tint[]): Tint {
  return tints.reduce((worst, tint) => (TINT_RANK[tint] > TINT_RANK[worst] ? tint : worst), "unknown");
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}
