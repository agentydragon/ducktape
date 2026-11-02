import { describe, expect, it } from "vitest";
import type { ProviderView } from "./schema";
import { effectiveQuota } from "./quotas";

describe("effectiveQuota", () => {
  it("returns the server-selected OpenAI Platform credits", () => {
    const credits = { total_available_usd: "25.75", total_paid_available_usd: "25.75", grants: [] };
    const provider = {
      provider: "openai_platform",
      last_output: {
        fetched_at: "2026-01-15T12:00:00Z",
        result: { kind: "success", windows: [], openai_platform_credits: credits },
      },
      display: {
        source: "current",
        observed_at: "2026-01-15T12:00:00Z",
        result: { kind: "success", windows: [], openai_platform_credits: credits },
        prepaid_balance: null,
        prepaid_stale_since: null,
      },
      display_windows: [],
      display_openai_platform_credits: credits,
      display_anthropic_platform_credits: null,
    } as unknown as ProviderView;

    expect(effectiveQuota(provider).openaiPlatformCredits).toEqual(credits);
  });

  it("renders the server-selected stale snapshot and Anthropic credits", () => {
    const provider = {
      provider: "anthropic_platform",
      last_output: { fetched_at: "2026-01-15T12:00:00Z", result: { kind: "error", error: "offline" } },
      display: {
        source: "last_success",
        observed_at: "2026-01-14T12:00:00Z",
        result: {
          kind: "success",
          windows: [],
          anthropic_platform_credits: {
            organizations: [{ uuid: "org-1", name: "Org", tranches: [], promo_tranches: [] }],
          },
          reset_credit_expiry_lookup_failed: false,
        },
        prepaid_balance: null,
        prepaid_stale_since: null,
      },
      display_windows: [],
      display_openai_platform_credits: null,
      display_anthropic_platform_credits: {
        organizations: [{ uuid: "org-1", name: "Org", tranches: [], promo_tranches: [] }],
      },
    } as unknown as ProviderView;

    const result = effectiveQuota(provider);

    expect(result.staleSince).toBe("2026-01-14T12:00:00Z");
    expect(result.error).toBe("offline");
    expect(result.anthropicPlatformCredits?.organizations[0]?.uuid).toBe("org-1");
  });
});
