import type { ExtraSpend, FetchSuccess, PaidCredits, ProviderView, QuotaWindow } from "./schema";

/**
 * The `/v1/quotas` payload and the one reading of it every surface performs.
 *
 * Types come from the API's own OpenAPI document (`//aiquota/frontend:schema`), so the wire
 * contract is the Pydantic models rather than a hand-kept copy of them.
 *
 * `effectiveQuota` is the "what do we actually show" step: a failed refresh with a good prior
 * snapshot displays the stale numbers rather than "no data", because stale-but-real beats
 * nothing — the same resolution `aiquota/render/human.py` and the GNOME popup make.
 */

export type PrepaidBalance = NonNullable<FetchSuccess["prepaid_balance"]>;
export type EffectiveQuota = {
  windows: QuotaWindow[];
  extraSpend: ExtraSpend | null;
  paidCredits: PaidCredits | null;
  prepaidBalance: PrepaidBalance | null;
  resetCredits: number | null;
  resetCreditExpiries: string[];
  resetCreditExpiryLookupFailed: boolean;
  /** When the shown numbers come from an older snapshot: when that snapshot was taken. */
  staleSince: string | null;
  error: string | null;
};

export function effectiveQuota(provider: ProviderView): EffectiveQuota {
  const result = provider.last_output.result;
  const error = result.kind === "error" ? result.error : null;
  if (
    result.kind === "success" &&
    (result.windows?.length ||
      result.available_reset_credits != null ||
      result.paid_credits != null ||
      result.prepaid_balance != null)
  ) {
    return { ...shown(result), staleSince: null, error };
  }
  const fallback = provider.last_success;
  if (fallback) return { ...shown(fallback.result), staleSince: fallback.fetched_at, error };
  return {
    windows: [],
    extraSpend: null,
    paidCredits: null,
    prepaidBalance: null,
    resetCredits: null,
    resetCreditExpiries: [],
    resetCreditExpiryLookupFailed: false,
    staleSince: null,
    error,
  };
}

/** Seconds until reset, counted from `now` where the provider gave an absolute instant. */
export function resetSeconds(window: QuotaWindow, now: number): number {
  if (!window.reset_at) return window.reset_seconds;
  return Math.max(0, (Date.parse(window.reset_at) - now) / 1000);
}

/** A window shorter than the provider's longest is the burst window: hitting it blocks now. */
export function isShortWindow(window: QuotaWindow, windows: QuotaWindow[]): boolean {
  return window.window_seconds < Math.max(...windows.map((candidate) => candidate.window_seconds));
}

function shown(result: FetchSuccess): Omit<EffectiveQuota, "staleSince" | "error"> {
  return {
    windows: (result.windows ?? []).filter((window) => window.display),
    extraSpend: result.extra_spend ?? null,
    paidCredits: result.paid_credits ?? null,
    prepaidBalance: result.prepaid_balance ?? null,
    resetCredits: result.available_reset_credits ?? null,
    resetCreditExpiries: result.available_reset_credit_expiries ?? [],
    resetCreditExpiryLookupFailed: result.reset_credit_expiry_lookup_failed ?? false,
  };
}
