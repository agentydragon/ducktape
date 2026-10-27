/** Browser adapters for the shared runtime-neutral presentation helpers. */

import {
  displayUsedPercent as sharedDisplayUsedPercent,
  formatAge as sharedFormatAge,
  formatClock as sharedFormatClock,
  formatDuration as sharedFormatDuration,
  formatKnownExpiry,
  formatMultiplier as sharedFormatMultiplier,
  formatPace as sharedFormatPace,
  formatPaceForecast as sharedFormatPaceForecast,
  formatPeakInterval as sharedFormatPeakInterval,
  formatPrepaidBalance as sharedFormatPrepaidBalance,
  formatWindowDuration as sharedFormatWindowDuration,
  formatWindowLabel as sharedFormatWindowLabel,
  roundHalfToEven,
} from "../shared/presentation.js";
import type { Pace } from "./pace";

export { roundHalfToEven };

export function formatDuration(seconds: number): string {
  return sharedFormatDuration(seconds);
}

export function formatWindowDuration(seconds: number): string {
  return sharedFormatWindowDuration(seconds);
}

export function formatWindowLabel(window: { name?: string | null; window_seconds: number }): string {
  return sharedFormatWindowLabel({ name: window.name, windowSeconds: window.window_seconds });
}

export function displayUsedPercent(window: { used_percent: number }): number {
  return sharedDisplayUsedPercent(window.used_percent);
}

export function formatAge(seconds: number): string {
  return sharedFormatAge(seconds);
}

export function formatPrepaidBalance(amountMinor: number, exponent: number, currency: string): string {
  return sharedFormatPrepaidBalance(amountMinor, exponent, currency);
}

export function formatPace(pace: Pace | null): string | null {
  return sharedFormatPace(pace);
}

export function formatPaceForecast(pace: Pace | null, resetSeconds: number): string | null {
  return sharedFormatPaceForecast(pace, resetSeconds);
}

export function formatClock(instant: Date): string {
  return sharedFormatClock(instant);
}

export function formatMultiplier(value: number): string {
  return sharedFormatMultiplier(value);
}

export function formatPeakInterval(interval: { start: string; end: string }): string {
  return sharedFormatPeakInterval(interval);
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
