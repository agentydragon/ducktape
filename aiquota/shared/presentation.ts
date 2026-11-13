import {
  formatLocalClock,
  formatLocalTimestamp,
  formatPeakInterval as sharedFormatPeakInterval,
} from "../../util/date_presentation.js";

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
  if (days > 0) return `${days}d${hours ? ` ${hours}h` : ""}`;
  if (hours > 0) return `${hours}h${minutes ? ` ${minutes}m` : ""}`;
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

export { formatLocalClock as formatClock };

export function formatPeakInterval(interval: { start: string; end: string }): string {
  return sharedFormatPeakInterval(interval);
}

export function formatDeadline(expiry: string | Date, now: Date, details = false): string | null {
  return formatLocalTimestamp(expiry, now, details);
}

// Keep the default visibility rule in sync with render/view_model.py.
export function showResetCredits(
  count: number | null,
  windows: { exhausted: boolean; resetPending: boolean }[],
  details = false
): boolean {
  return (
    count !== null &&
    (count > 0 ||
      details ||
      (windows.some((window) => window.exhausted) && !windows.some((window) => window.resetPending)))
  );
}

export function formatResetCreditSummary(
  count: number,
  expiries: (string | Date)[],
  expiryLookupFailed: boolean,
  now: Date,
  details = false
): string {
  if (count === 0) return "0";
  const parts = [String(count)];
  const known = expiries
    .map((expiry) => new Date(expiry))
    .filter((date) => !Number.isNaN(date.getTime()))
    .sort((a, b) => a.getTime() - b.getTime());
  if (known.length) {
    const shown = details ? known : known.slice(0, 1);
    parts.push(
      `${details ? "known expiries" : "next known expiry"} ${shown.map((date) => formatDeadline(date, now, details)).join(", ")}`
    );
  }
  if (expiryLookupFailed) parts.push("expiry lookup failed");
  else if (count > known.length) {
    const missing = count - known.length;
    parts.push(`${missing} ${missing === 1 ? "expiry" : "expiries"} unknown`);
  }
  return parts.join(" · ");
}

export function formatMultiplier(value: number): string {
  return `${value}x`;
}

function currencyPrecision(amount: number): number {
  const [mantissa, exponent] = Math.abs(amount).toString().split("e-");
  const fraction = (mantissa.split(".")[1] ?? "").length;
  return Math.max(2, Math.min(20, fraction + (exponent ? Number(exponent) : 0)));
}

export function formatUsd(amount: number): string {
  if (amount !== 0 && Math.abs(amount) < 1e-20) return `${amount < 0 ? "-" : ""}$${Math.abs(amount).toExponential()}`;
  const precision = currencyPrecision(amount);
  return `${amount < 0 ? "-" : ""}$${Math.abs(amount).toLocaleString("en-US", { minimumFractionDigits: precision, maximumFractionDigits: precision })}`;
}

export function formatUsdWhole(amount: number): string {
  return `${amount < 0 ? "-" : ""}$${Math.round(Math.abs(amount)).toLocaleString("en-US")}`;
}

function formatUsdCents(amount: number): string {
  return `${amount < 0 ? "-" : ""}$${Math.abs(amount).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function sameToCents(first: number, second: number): boolean {
  return formatUsdCents(first) === formatUsdCents(second);
}

function summaryHidesCents(amount: number): boolean {
  return Math.abs(amount) >= 10 && formatUsdCents(amount).replace(/\.00$/, "") !== formatUsdWhole(amount);
}

export function formatExtraSpend(usedUsd: number, limitUsd: number, enabled = false): string {
  let used = formatSummaryUsd(usedUsd);
  let limit = formatSummaryUsd(limitUsd);
  let qualifier = "";
  if (!sameToCents(usedUsd, limitUsd) && used === limit) {
    used = formatUsdCents(usedUsd);
    limit = formatUsdCents(limitUsd);
  } else if (usedUsd !== limitUsd && used === limit) {
    qualifier = ` · ${usedUsd > limitUsd ? "over" : "under"} by <$0.01`;
  }
  return `Extra spend this month: ${used} of ${limit} limit${qualifier}${enabled ? " · extra usage enabled" : ""}`;
}

export function formatExtraSpendExact(usedUsd: number, limitUsd: number): string {
  return `${formatUsd(usedUsd)} of ${formatUsd(limitUsd)} limit`;
}

export function extraSpendHasHiddenFraction(usedUsd: number, limitUsd: number): boolean {
  const used = formatSummaryUsd(usedUsd);
  const limit = formatSummaryUsd(limitUsd);
  if (!sameToCents(usedUsd, limitUsd) && used === limit) return false; // The summary expands cent-level collisions.
  return [usedUsd, limitUsd].some(summaryHidesCents);
}

export function formatSummaryUsd(amount: number): string {
  if (amount === 0) return "$0";
  if (Math.abs(amount) < 0.005) return `${amount < 0 ? "-" : ""}<$0.01`;
  if (Math.abs(amount) < 10) {
    return formatUsdCents(amount).replace(/\.00$/, "");
  }
  return `${amount < 0 ? "-" : ""}${formatUsdWhole(Math.abs(amount))}`;
}

export function staleObservationAt(
  observedAt: number | null,
  staleSince: number | null,
  staleAt: number | null,
  now: number
): number | null {
  return staleSince ?? (staleAt !== null && now >= staleAt ? observedAt : null);
}

export type WindowTiming = {
  resetAtMs: number | null;
  resetSeconds: number | null;
  resetPending: boolean;
  reset: string;
  pace: string | null;
  forecast: string | null;
};

/** Advance deadlines without replacing provider-reported usage or inferring a refilled bucket. */
export function refreshWindowTiming<T extends WindowTiming>(window: T, now: number, stale = false): T {
  const deadline = window.resetAtMs;
  const hasDeadline = deadline !== null && Number.isFinite(deadline);
  const resetSeconds = hasDeadline ? Math.max(0, (deadline - now) / 1000) : window.resetSeconds;
  const resetPending = window.resetPending || (hasDeadline && now >= deadline);
  return {
    ...window,
    resetSeconds,
    resetPending,
    reset: resetPending
      ? "awaiting update"
      : hasDeadline && resetSeconds !== null
        ? formatDuration(resetSeconds)
        : window.reset,
    pace: resetPending || stale ? null : window.pace,
    forecast: resetPending || stale ? null : window.forecast,
  };
}

/** Recheck current quota signals against live freshness and every displayed reset deadline. */
export function quotaActivity(
  windows: { exhausted: boolean; resetPending: boolean }[],
  stale: boolean,
  overPlan: boolean,
  paidCredits: boolean,
  hasMonthlySpendRow = false
): { resetPending: boolean; overPlan: boolean; paidCredits: boolean; status: string | null } {
  const resetPending = windows.some((window) => window.resetPending);
  const current = !stale && !resetPending;
  const activity = { resetPending, overPlan: current && overPlan, paidCredits: current && paidCredits };
  const exhausted = windows.some((window) => window.exhausted);
  const status = resetPending
    ? "Reset time passed · awaiting updated usage"
    : exhausted && activity.overPlan && !hasMonthlySpendRow
      ? "Extra usage enabled"
      : null;
  return { ...activity, status };
}
