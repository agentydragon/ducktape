export type Tint = "cool" | "ok" | "warn" | "hot" | "unknown" | "stale";

export type Pace = {
  deviation: number;
  projectedAtReset: number | null;
  secondsToExhaust: number | null;
  stable: boolean;
};

export type WindowMath = { usedPercent: number; resetSeconds: number; windowSeconds: number };

export function roundHalfToEven(value: number): number;
export function isExhausted(window: WindowMath): boolean;
export function elapsedFraction(window: WindowMath): number;
export function computePace(window: WindowMath): Pace;
export function tintFor(pace: Pace | null, usedPercent: number | null, isShort: boolean): Tint;
export function bindingTint(tints: Tint[]): Tint;
export function formatDuration(seconds: number): string;
export function formatWindowDuration(seconds: number): string;
export function formatWindowLabel(window: { name?: string | null; windowSeconds: number }): string;
export function displayUsedPercent(usedPercent: number): number;
export function formatAge(seconds: number): string;
export function formatPace(pace: Pace | null): string | null;
export function formatPaceForecast(pace: Pace | null, resetSeconds: number): string | null;
export function formatPrepaidBalance(amountMinor: number, exponent: number, currency: string): string;
export function formatClock(instant: Date): string;
export function formatPeakInterval(interval: { start: string | Date; end: string | Date }): string;
export function formatKnownExpiry(expiry: string | Date): string | null;
export function formatMultiplier(value: number): string;
