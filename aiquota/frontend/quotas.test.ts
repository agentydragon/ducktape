/**
 * What the dashboard shows when the latest refresh did not go well — the resolution
 * `aiquota/render/human.py` and the GNOME popup also make, restated here because a browser
 * that silently swapped a good snapshot for "no data" on one failed poll would be worse than
 * one that never refreshed.
 */

import { describe, expect, it } from "vitest";

import type { ProviderView, QuotaWindow, WindowView } from "./schema";
import { effectiveQuota } from "./quotas";

const FETCHED_AT = "2026-01-15T12:00:00Z";
const EARLIER = "2026-01-14T12:00:00Z";

function window(overrides: Partial<QuotaWindow> = {}): QuotaWindow {
  return { name: null, display: true, used_percent: 40, reset_seconds: 3600, window_seconds: 18000, ...overrides };
}

function provider(overrides: Partial<ProviderView>): ProviderView {
  const result = overrides.last_output?.result;
  const current =
    result?.kind === "success" &&
    (result.windows?.length ||
      result.available_reset_credits != null ||
      result.paid_credits != null ||
      result.prepaid_balance != null);
  const windows =
    result === undefined
      ? [window()]
      : current && result.kind === "success"
        ? result.windows
        : (overrides.last_success?.result.windows ?? []);
  const display: WindowView[] = (windows ?? [])
    .filter((w) => w.display)
    .map((w) => ({
      ...w,
      label: "5h",
      used: w.used_percent,
      reset: "1h00m",
      pace: null,
      forecast: null,
      tint: "ok",
      elapsed: 0.5,
      exhausted: false,
    }));
  return {
    provider: "claude",
    last_output: {
      fetched_at: FETCHED_AT,
      result: { kind: "success", windows: [window()], reset_credit_expiry_lookup_failed: false },
    },
    last_success: null,
    currently_over_plan: false,
    paid_credits_active: false,
    extra_status: "none",
    burn: null,
    display_windows: display,
    summary_tint: "unknown",
    summary_index: null,
    ...overrides,
  };
}

describe("effectiveQuota", () => {
  it("shows the latest windows when the refresh succeeded", () => {
    const quota = effectiveQuota(provider({}));
    expect(quota.windows).toHaveLength(1);
    expect(quota.staleSince).toBeNull();
    expect(quota.error).toBeNull();
  });

  it("falls back to the last good snapshot, dated, when the refresh failed", () => {
    const quota = effectiveQuota(
      provider({
        last_output: { fetched_at: FETCHED_AT, result: { kind: "error", error: "HTTP 503" } },
        last_success: {
          fetched_at: EARLIER,
          result: {
            kind: "success",
            windows: [window({ used_percent: 72 })],
            reset_credit_expiry_lookup_failed: false,
          },
        },
      })
    );
    expect(quota.windows.map((shown) => shown.used_percent)).toEqual([72]);
    expect(quota.staleSince).toBe(EARLIER);
    expect(quota.error).toBe("HTTP 503");
  });

  it("reports nothing to show when a failure has no snapshot behind it", () => {
    const quota = effectiveQuota(
      provider({ last_output: { fetched_at: FETCHED_AT, result: { kind: "error", error: "no credentials" } } })
    );
    expect(quota.windows).toEqual([]);
    expect(quota.staleSince).toBeNull();
  });

  it("keeps a windowless success that still reports banked resets", () => {
    // Codex answers 200 with no rate_limit but a live reset-credit count; that is current
    // data, not a reason to fall back to an older snapshot.
    const quota = effectiveQuota(
      provider({
        last_output: {
          fetched_at: FETCHED_AT,
          result: {
            kind: "success",
            windows: [],
            available_reset_credits: 2,
            reset_credit_expiry_lookup_failed: false,
          },
        },
        last_success: {
          fetched_at: EARLIER,
          result: { kind: "success", windows: [window()], reset_credit_expiry_lookup_failed: false },
        },
      })
    );
    expect(quota.resetCredits).toBe(2);
    expect(quota.windows).toEqual([]);
    expect(quota.staleSince).toBeNull();
  });

  it("retains the banked reset count when supplemental expiry details failed", () => {
    const quota = effectiveQuota(
      provider({
        last_output: {
          fetched_at: FETCHED_AT,
          result: {
            kind: "success",
            windows: [window()],
            available_reset_credits: 2,
            reset_credit_expiry_lookup_failed: true,
          },
        },
      })
    );

    expect(quota.resetCredits).toBe(2);
    expect(quota.resetCreditExpiryLookupFailed).toBe(true);
  });

  it("drops windows the provider marked as not for display", () => {
    const quota = effectiveQuota(
      provider({
        last_output: {
          fetched_at: FETCHED_AT,
          result: {
            kind: "success",
            windows: [window(), window({ window_seconds: 604800, display: false })],
            reset_credit_expiry_lookup_failed: false,
          },
        },
      })
    );
    expect(quota.windows.map((shown) => shown.window_seconds)).toEqual([18000]);
  });
});
