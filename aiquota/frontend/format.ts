import { formatKnownExpiry, roundHalfToEven } from "../shared/presentation.js";

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
