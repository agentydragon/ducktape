import type { ExtraSpend, ProviderView, WindowView } from "./schema";

/** Fields from the server-selected snapshot that the board renders beside normalized rows. */
export type EffectiveQuota = {
  windows: WindowView[];
  extraSpend: ExtraSpend | null;
  resetCredits: number | null;
  resetCreditExpiries: string[];
  resetCreditExpiryLookupFailed: boolean;
  staleSince: string | null;
  error: string | null;
};

export function effectiveQuota(provider: ProviderView): EffectiveQuota {
  const result = provider.display.result;
  return {
    windows: provider.display_windows ?? [],
    extraSpend: provider.monthly_extra_spend ?? null,
    resetCredits: result?.available_reset_credits ?? null,
    resetCreditExpiries: result?.available_reset_credit_expiries ?? [],
    resetCreditExpiryLookupFailed: result?.reset_credit_expiry_lookup_failed ?? false,
    staleSince: provider.display.source === "last_success" ? provider.display.observed_at : null,
    error: provider.last_output.result.kind === "error" ? provider.last_output.result.error : null,
  };
}
