import type { ExtraSpend, FetchSuccess, PaidCredits, ProviderView, WindowView } from "./schema";

/** Render the server-selected quota snapshot and its freshness. */
export type PrepaidBalance = NonNullable<FetchSuccess["prepaid_balance"]>;
export type EffectiveQuota = {
  windows: WindowView[];
  extraSpend: ExtraSpend | null;
  paidCredits: PaidCredits | null;
  platformCredits: FetchSuccess["platform_credits"];
  anthropicPlatformCredits: FetchSuccess["anthropic_platform_credits"];
  prepaidBalance: PrepaidBalance | null;
  prepaidStaleSince: string | null;
  resetCredits: number | null;
  resetCreditExpiries: string[];
  resetCreditExpiryLookupFailed: boolean;
  /** When the shown numbers come from an older snapshot: when that snapshot was taken. */
  staleSince: string | null;
  error: string | null;
};

export function effectiveQuota(provider: ProviderView): EffectiveQuota {
  const result = provider.display.result;
  const error = provider.last_output.result.kind === "error" ? provider.last_output.result.error : null;
  return {
    windows: provider.display_windows,
    extraSpend: result?.extra_spend ?? null,
    paidCredits: result?.paid_credits ?? null,
    platformCredits: provider.display_platform_credits ?? null,
    anthropicPlatformCredits: provider.display_anthropic_platform_credits ?? null,
    prepaidBalance: provider.display.prepaid_balance ?? null,
    prepaidStaleSince: provider.display.prepaid_stale_since ?? null,
    resetCredits: result?.available_reset_credits ?? null,
    resetCreditExpiries: result?.available_reset_credit_expiries ?? [],
    resetCreditExpiryLookupFailed: result?.reset_credit_expiry_lookup_failed ?? false,
    staleSince: provider.display.source === "last_success" ? provider.display.observed_at : null,
    error,
  };
}
