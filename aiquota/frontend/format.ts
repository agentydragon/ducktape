/**
 * Display strings for the browser dashboard — the TypeScript half of
 * `aiquota/render/format.py`, which is the canonical statement of every format here.
 *
 * The dashboard, the CLI and the GNOME popup are three presentations of one state, and the
 * user reads them side by side; a window that is "3h30m from reset" in the terminal must not
 * be "3 hours" in the browser. `format.test.ts` pins this port to what the Python renderers
 * produce for the shared scenarios (`aiquota/testing/fixtures/`).
 *
 * Instants are formatted in the viewer's zone on purpose: vendors publish peak hours in their
 * own, and converting them is the reason these are surfaced at all.
 */

import type { Pace } from "./pace";

/**
 * Python's `round`, which every format here mirrors: a .5 tie goes to the even integer, where
 * JavaScript's `Math.round` goes up. It bites about as often as a percentage lands exactly on
 * a half — a 12.5% surplus prints as 12% in the terminal, and would print as 13% here.
 */
export function roundHalfToEven(value: number): number {
  const floor = Math.floor(value);
  const fraction = value - floor;
  if (fraction > 0.5) return floor + 1;
  if (fraction < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** Compact time-to-go: `2d3h`, `3h30m`, `45m`. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, roundHalfToEven(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d${hours}h`;
  if (hours > 0) return `${hours}h${String(minutes).padStart(2, "0")}m`;
  return `${minutes}m`;
}

/** A window's length in its own unit: `5h`, `7d`. */
export function formatWindowDuration(seconds: number): string {
  const rounded = roundHalfToEven(seconds);
  if (rounded % 86400 === 0) return `${rounded / 86400}d`;
  if (rounded % 3600 === 0) return `${rounded / 3600}h`;
  if (rounded % 60 === 0) return `${rounded / 60}m`;
  return `${rounded}s`;
}

export function formatWindowLabel(window: { name?: string | null; window_seconds: number }): string {
  const duration = formatWindowDuration(window.window_seconds);
  return window.name ? `${window.name} (${duration})` : duration;
}

/**
 * Usage as shown, capped below 100 until the window really is exhausted: rounding 99.6% to
 * "100%" would claim a window is spent while calls still go through.
 */
export function displayUsedPercent(window: { used_percent: number }): number {
  const rounded = roundHalfToEven(window.used_percent);
  return window.used_percent >= 100 ? rounded : Math.min(rounded, 99);
}

export function formatAge(seconds: number): string {
  const total = Math.max(0, roundHalfToEven(seconds));
  return total < 60 ? `${total}s` : formatDuration(total);
}

/** Signed deviation from the constant-rate line; null while the window is too young to say. */
export function formatPace(pace: Pace | null): string | null {
  if (pace === null || !pace.stable) return null;
  return `${pace.deviation >= 0 ? "+" : "-"}${Math.abs(roundHalfToEven(pace.deviation))}%`;
}

export function formatPaceForecast(pace: Pace | null, resetSeconds: number): string | null {
  if (pace === null || !pace.stable || pace.projectedAtReset === null) return null;
  if (pace.projectedAtReset > 100.5 && pace.secondsToExhaust !== null) {
    return `exhausts ~${formatDuration(resetSeconds - pace.secondsToExhaust)} before reset`;
  }
  if (pace.projectedAtReset < 95) return `leaves ~${roundHalfToEven(100 - pace.projectedAtReset)}% unused at reset`;
  return "on pace";
}

export function formatClock(instant: Date): string {
  return instant.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function formatMultiplier(value: number): string {
  return `${value}x`;
}

export function formatPeakInterval(interval: { start: string; end: string }): string {
  const start = new Date(interval.start);
  const weekday = start.toLocaleDateString([], { weekday: "short" });
  return `${weekday} ${formatClock(start)}-${formatClock(new Date(interval.end))}`;
}

export function formatKnownExpiries(expiries: string[]): string {
  return expiries
    .map((expiry) =>
      new Date(expiry).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    )
    .join(", ");
}

export function formatUsd(amount: number): string {
  return `$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** A plan limit, which is always a round figure: `$4,600`. */
export function formatUsdWhole(amount: number): string {
  return `$${roundHalfToEven(amount).toLocaleString("en-US")}`;
}
