import {
  displayUsedPercent as displaySharedUsedPercent,
  formatKnownExpiry,
  formatWindowLabel as formatSharedWindowLabel,
  roundHalfToEven,
} from "../shared/presentation.js";

/** Adapt the API's snake_case window shape to the shared presentation model. */
export function formatWindowLabel(window: { name?: string | null; window_seconds: number }): string {
  return formatSharedWindowLabel({ name: window.name, windowSeconds: window.window_seconds });
}

/** Adapt the API's snake_case window shape to the shared presentation model. */
export function displayUsedPercent(window: { used_percent: number }): number {
  return displaySharedUsedPercent(window.used_percent);
}

export function formatKnownExpiries(expiries: string[]): string {
  return expiries.map((expiry) => formatKnownExpiry(expiry) ?? "Invalid Date").join(", ");
}

export function formatUsd(amount: number): string {
  return `$${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** A plan limit, which is always a round figure: `$4,600`. */
export function formatUsdWhole(amount: number): string {
  return `$${roundHalfToEven(amount).toLocaleString("en-US")}`;
}
