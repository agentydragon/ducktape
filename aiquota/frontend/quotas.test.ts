import { describe, expect, it } from "vitest";
import type { ProviderView } from "./schema";
import { effectiveQuota } from "./quotas";

describe("effectiveQuota", () => {
  it("uses the selected cached snapshot while preserving the latest refresh error", () => {
    const provider = {
      provider: "codex",
      display: {
        source: "last_success",
        observed_at: "2026-01-14T12:00:00Z",
        result: {
          kind: "success",
          windows: [],
          extra_spend: { used_usd: 25.5, monthly_limit_usd: 100, utilization: 25.5 },
          available_reset_credits: 2,
          available_reset_credit_expiries: ["2026-02-01T12:00:00Z"],
          reset_credit_expiry_lookup_failed: false,
        },
      },
      last_output: { fetched_at: "2026-01-15T12:00:00Z", result: { kind: "error", error: "offline" } },
      monthly_extra_spend: { used_usd: 25.5, monthly_limit_usd: 100, utilization: 25.5 },
      display_windows: [],
    } as unknown as ProviderView;

    expect(effectiveQuota(provider)).toEqual({
      windows: [],
      extraSpend: { used_usd: 25.5, monthly_limit_usd: 100, utilization: 25.5 },
      resetCredits: 2,
      resetCreditExpiries: ["2026-02-01T12:00:00Z"],
      resetCreditExpiryLookupFailed: false,
      staleSince: "2026-01-14T12:00:00Z",
      error: "offline",
    });
  });

  it("handles newly optional cached presentation fields without inventing readings", () => {
    const provider = {
      provider: "codex",
      display: { source: "none", observed_at: null, result: null },
      last_output: { fetched_at: "2026-01-15T12:00:00Z", result: { kind: "error", error: "offline" } },
    } as unknown as ProviderView;

    expect(effectiveQuota(provider)).toEqual({
      windows: [],
      extraSpend: null,
      resetCredits: null,
      resetCreditExpiries: [],
      resetCreditExpiryLookupFailed: false,
      staleSince: null,
      error: "offline",
    });
  });
});
