export type Tint = "cool" | "ok" | "warn" | "hot" | "unknown" | "stale";

export function roundHalfToEven(value: number): number {
  const floor = Math.floor(value);
  const fraction = value - floor;
  if (fraction > 0.5) return floor + 1;
  if (fraction < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
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

export function formatAge(seconds: number): string {
  const total = Math.max(0, roundHalfToEven(seconds));
  return total < 60 ? `${total}s` : formatDuration(total);
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
