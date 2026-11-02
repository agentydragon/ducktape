import { describe, expect, it } from "vitest";
import type { ProviderView } from "./schema";
import { effectiveQuota } from "./quotas";

describe("effectiveQuota", () => {
  it("renders the server-selected stale snapshot even when current data differs", () => {
    const provider = {
      provider: "anthropic_platform",
      last_output: { fetched_at: "2026-01-15T12:00:00Z", result: { kind: "error", error: "offline" } },
      display: {
        source: "last_success",
        observed_at: "2026-01-14T12:00:00Z",
        result: { kind: "success", windows: [], available_reset_credits: 0, reset_credit_expiry_lookup_failed: false },
        prepaid_balance: null,
        prepaid_stale_since: null,
      },
      display_windows: [],
      display_platform_credits: null,
      display_anthropic_platform_credits: {
        organizations: [{ uuid: "org-1", name: "Org", tranches: [], promo_tranches: [] }],
      },
    } as unknown as ProviderView;

    const result = effectiveQuota(provider);

    expect(result.resetCredits).toBe(0);
    expect(result.staleSince).toBe("2026-01-14T12:00:00Z");
    expect(result.error).toBe("offline");
    expect(result.anthropicPlatformCredits?.organizations[0]?.uuid).toBe("org-1");
  });
});
