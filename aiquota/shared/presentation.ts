export type Tint = "cool" | "ok" | "warn" | "hot" | "unknown" | "stale";

export type Pace = {
  deviation: number;
  projectedAtReset: number | null;
  secondsToExhaust: number | null;
  stable: boolean;
};

export type WindowMath = { usedPercent: number; resetSeconds: number; windowSeconds: number };

export function roundHalfToEven(value: number): number {
  const floor = Math.floor(value);
  const fraction = value - floor;
  if (fraction > 0.5) return floor + 1;
  if (fraction < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

export function isExhausted({ usedPercent }: WindowMath): boolean {
  return usedPercent >= 100;
}

export function elapsedFraction({ resetSeconds, windowSeconds }: WindowMath): number {
  return clamp01((windowSeconds - resetSeconds) / windowSeconds);
}

export function computePace({ usedPercent, resetSeconds, windowSeconds }: WindowMath): Pace {
  const elapsedSeconds = windowSeconds - resetSeconds;
  const elapsed = elapsedSeconds / windowSeconds;
  const spending = elapsedSeconds > 0 && usedPercent > 0;
  const ratePerSecond = usedPercent / elapsedSeconds;
  return {
    deviation: usedPercent - elapsed * 100,
    projectedAtReset: spending ? usedPercent + ratePerSecond * resetSeconds : null,
    secondsToExhaust: spending ? (100 - usedPercent) / ratePerSecond : null,
    stable: elapsed > 0.05 && elapsed < 0.95,
  };
}

export function tintFor(pace: Pace | null, usedPercent: number | null, isShort: boolean): Tint {
  if (usedPercent == null) return "unknown";
  if (isShort && usedPercent >= 85) return "hot";
  if (pace === null || !pace.stable) {
    if (usedPercent >= 95) return "hot";
    if (usedPercent >= 80) return "warn";
    return "ok";
  }
  if (pace.deviation >= 15) return "hot";
  if (pace.deviation >= 5) return "warn";
  if (pace.deviation <= -10) return "cool";
  return "ok";
}

export function bindingTint(tints: Tint[]): Tint {
  const rank: Record<Tint, number> = { unknown: 0, stale: 0, ok: 1, cool: 1, warn: 2, hot: 3 };
  return tints.reduce((worst, tint) => (rank[tint] > rank[worst] ? tint : worst), "unknown");
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, roundHalfToEven(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d${hours}h`;
  if (hours > 0) return `${hours}h${String(minutes).padStart(2, "0")}m`;
  return `${minutes}m`;
}

export function formatWindowDuration(seconds: number): string {
  const rounded = roundHalfToEven(seconds);
  if (rounded % 86400 === 0) return `${rounded / 86400}d`;
  if (rounded % 3600 === 0) return `${rounded / 3600}h`;
  if (rounded % 60 === 0) return `${rounded / 60}m`;
  return `${rounded}s`;
}

export function formatWindowLabel(window: { name?: string | null; windowSeconds: number }): string {
  const duration = formatWindowDuration(window.windowSeconds);
  return window.name ? `${window.name} (${duration})` : duration;
}

export function displayUsedPercent(usedPercent: number): number {
  const rounded = roundHalfToEven(usedPercent);
  return usedPercent >= 100 ? rounded : Math.min(rounded, 99);
}

export function formatAge(seconds: number): string {
  const total = Math.max(0, roundHalfToEven(seconds));
  return total < 60 ? `${total}s` : formatDuration(total);
}

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

export function formatPrepaidBalance(amountMinor: number, exponent: number, currency: string): string {
  const negative = amountMinor < 0;
  const amount = BigInt(Math.abs(amountMinor));
  const scale = 10n ** BigInt(exponent);
  const whole = (amount / scale).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = exponent > 0 ? `.${(amount % scale).toString().padStart(exponent, "0")}` : "";
  const value = `${whole}${fraction}`;
  const sign = negative ? "-" : "";
  return currency === "USD" ? `${sign}$${value}` : `${sign}${value} ${currency}`;
}

export function formatClock(instant: Date): string {
  return instant.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function formatPeakInterval(interval: { start: string; end: string }): string {
  const start = new Date(interval.start);
  const weekday = start.toLocaleDateString([], { weekday: "short" });
  return `${weekday} ${formatClock(start)}-${formatClock(new Date(interval.end))}`;
}

export function formatKnownExpiry(expiry: string | Date): string | null {
  const date = expiry instanceof Date ? expiry : new Date(expiry);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function formatMultiplier(value: number): string {
  return `${value}x`;
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}
