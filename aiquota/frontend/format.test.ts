/**
 * Holds this package's formatters to the Python ones they are a port of.
 *
 * `aiquota/render/format.py` and `aiquota/pace.py` are canonical. Both JavaScript surfaces
 * consume `aiquota/shared/presentation.js`; these cases hold its browser adapters to Python's
 * output for the shared scenarios. The GNOME render test also loads the packaged copy.
 */

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import {
  formatDuration,
  formatExtraSpend,
  extraSpendHasHiddenFraction,
  refreshWindowTiming,
  quotaActivity,
  staleObservationAt,
  formatDeadline,
  formatResetCreditSummary,
  formatPrepaidBalance,
  formatSummaryUsd,
  roundHalfToEven,
} from "../shared/presentation.js";

beforeAll(() => vi.stubEnv("TZ", "UTC"));
afterAll(() => vi.unstubAllEnvs());

describe("formatPrepaidBalance", () => {
  it.each([
    [12345, 2, "USD", "$123.45"],
    [123, 0, "USD", "$123"],
    [1234567, 3, "EUR", "1,234.567 EUR"],
    [0, 2, "USD", "$0.00"],
    [-123, 2, "USD", "-$1.23"],
  ])("matches Python's exact minor-unit rendering for %s %s %s", (amountMinor, exponent, currency, expected) => {
    expect(formatPrepaidBalance(amountMinor, exponent, currency)).toBe(expected);
  });
});

describe("Python's ties-to-even rounding", () => {
  it.each([
    [2.5, 2],
    [3.5, 4],
    [-2.5, -2],
    [-3.5, -4],
  ])("rounds %s to %s", (value, expected) => {
    expect(roundHalfToEven(value)).toBe(expected);
  });

  it("uses ties-to-even in the shared visible quota formats", () => {
    expect(formatDuration(3598.5)).toBe("59m");
  });
});

describe("formatResetCreditSummary", () => {
  const firstExpiry = "2026-01-01T15:20:00Z";
  const secondExpiry = "2026-02-02T12:34:00Z";
  const now = new Date("2026-01-15T12:00:00Z");

  it("keeps the main summary short and puts the complete known list in details", () => {
    expect(formatResetCreditSummary(2, [firstExpiry, secondExpiry], false, now)).toBe(
      "2 · next known expiry Jan 1, 15:20"
    );
    expect(formatResetCreditSummary(2, [firstExpiry, secondExpiry], false, now, true)).toBe(
      "2 · known expiries 2026-01-01 15:20 UTC, 2026-02-02 12:34 UTC"
    );
  });

  it("shows the difference between the count and a successful partial expiry lookup", () => {
    expect(formatResetCreditSummary(2, [firstExpiry], false, now)).toBe(
      "2 · next known expiry Jan 1, 15:20 · 1 expiry unknown"
    );
    expect(formatResetCreditSummary(1, [firstExpiry], false, now)).toBe("1 · next known expiry Jan 1, 15:20");
    expect(formatResetCreditSummary(2, [], false, now)).toBe("2 · 2 expiries unknown");
  });

  it("does not infer an unknown count when the expiry lookup fails", () => {
    expect(formatResetCreditSummary(2, [], true, now)).toBe("2 · expiry lookup failed");
    expect(formatResetCreditSummary(2, [firstExpiry], true, now)).toContain("expiry lookup failed");
  });

  it("keeps a zero reset count concise when the lookup succeeds", () => {
    expect(formatResetCreditSummary(0, [firstExpiry], false, now)).toBe("0");
  });
});

describe("formatDeadline", () => {
  const now = new Date("2026-01-15T12:00:00Z");

  it("uses a local date, includes the year when needed, and keeps nearby minute precision", () => {
    expect(formatDeadline("2026-02-02T12:34:00Z", now)).toBe("Feb 2");
    expect(formatDeadline("2027-02-02T12:34:00Z", now)).toBe("Feb 2, 2027");
    expect(formatDeadline("2026-01-15T12:34:00Z", now)).toBe("Jan 15, 12:34");
  });

  it("uses the 14-day cutoff and gives grant details the full local timestamp", () => {
    expect(formatDeadline("2026-01-29T11:59:59Z", now)).toBe("Jan 29, 11:59");
    expect(formatDeadline("2026-01-29T12:00:00Z", now)).toBe("Jan 29, 12:00");
    expect(formatDeadline("2026-01-29T12:00:01Z", now)).toBe("Jan 29");
    expect(formatDeadline("2026-01-01T12:00:00Z", now)).toBe("Jan 1, 12:00");
    expect(formatDeadline("2026-01-01T11:59:59Z", now)).toBe("Jan 1");
    expect(formatDeadline("2026-02-02T12:34:00Z", now, true)).toBe("2026-02-02 12:34 UTC");
  });

  it("returns null for an invalid deadline", () => {
    expect(formatDeadline("not a timestamp", now)).toBeNull();
  });

  it("uses the viewer zone across a daylight-saving transition", () => {
    vi.stubEnv("TZ", "America/Los_Angeles");
    try {
      expect(formatDeadline("2026-03-08T09:30:00Z", new Date("2026-03-08T08:00:00Z"))).toBe("Mar 8, 01:30");
      expect(formatDeadline("2026-03-08T10:30:00Z", new Date("2026-03-08T08:00:00Z"))).toBe("Mar 8, 03:30");
    } finally {
      vi.stubEnv("TZ", "UTC");
    }
  });
});

describe("summary currency", () => {
  it.each([
    [0, "$0"],
    [0.001, "$0.001"],
    [0.00000001, "$0.00000001"],
    [1e-21, "$1e-21"],
    [-1e-21, "-$1e-21"],
    [-0.01, "-$0.01"],
    [9.99, "$9.99"],
    [10.49, "$10"],
    [10.5, "$11"],
    [-10.5, "-$11"],
    [11.5, "$12"],
  ])("formats %s as %s", (amount, expected) => {
    expect(formatSummaryUsd(amount)).toBe(expected);
  });

  it("shows exact values when rounding would hide a difference from the limit", () => {
    expect(formatExtraSpend(10.4, 10.49)).toBe("Extra spend this month: $10.40 of $10.49 limit");
    expect(extraSpendHasHiddenFraction(10.4, 10.49)).toBe(false);
    expect(extraSpendHasHiddenFraction(20, 100)).toBe(false);
    expect(extraSpendHasHiddenFraction(20.4, 100)).toBe(true);
  });
});

describe("shared live presentation", () => {
  const now = Date.parse("2026-01-15T12:00:00Z");
  const window = {
    used_percent: 100,
    resetAtMs: now + 7200_000,
    resetSeconds: 7200,
    resetPending: false,
    reset: "2h",
    pace: "+5 pp",
    forecast: "exhausts early",
  };

  it("advances reset countdowns and marks a passed deadline without changing the last reported usage", () => {
    expect(refreshWindowTiming(window, now + 60_000).reset).toBe("1h 59m");
    const pending = refreshWindowTiming(window, now + 7200_000);
    expect(pending.resetPending).toBe(true);
    expect(pending.reset).toBe("awaiting update");
    expect(pending.used_percent).toBe(100);
    expect(pending.pace).toBeNull();
    expect(pending.forecast).toBeNull();
    expect(window.resetPending).toBe(false);
  });

  it("suppresses projections at the server freshness deadline while preserving the original observation", () => {
    expect(staleObservationAt(now, null, now + 600_000, now + 599_999)).toBeNull();
    expect(staleObservationAt(now, null, now + 600_000, now + 600_000)).toBe(now);
    expect(staleObservationAt(now, now - 60_000, null, now)).toBe(now - 60_000);
    const stale = refreshWindowTiming(window, now + 60_000, true);
    expect(stale.reset).toBe("1h 59m");
    expect(stale.resetPending).toBe(false);
    expect(stale.pace).toBeNull();
    expect(stale.forecast).toBeNull();
  });

  it("renders monthly spend identically for browser and GNOME", () => {
    expect(formatExtraSpend(2324.85, 4600)).toBe("Extra spend this month: $2,325 of $4,600 limit");
    expect(formatExtraSpend(2324.85, 4600, true)).toBe(
      "Extra spend this month: $2,325 of $4,600 limit · extra usage enabled"
    );
  });
});

describe("current paid signals", () => {
  it("keeps the distinct enabled signal and suppresses duplicate status across stale and pending readings", () => {
    const windows = [
      { exhausted: true, resetPending: false },
      { exhausted: true, resetPending: false },
    ];
    expect(quotaActivity(windows, false, true, true)).toEqual({
      resetPending: false,
      overPlan: true,
      paidCredits: true,
      status: "Extra usage enabled",
    });
    expect(quotaActivity(windows, false, true, true, true)).toEqual({
      resetPending: false,
      overPlan: true,
      paidCredits: true,
      status: null,
    });
    expect(quotaActivity(windows, true, true, true)).toEqual({
      resetPending: false,
      overPlan: false,
      paidCredits: false,
      status: null,
    });
    windows[0]!.resetPending = true;
    expect(quotaActivity(windows, false, true, true)).toEqual({
      resetPending: true,
      overPlan: false,
      paidCredits: false,
      status: "Reset time passed · awaiting updated usage",
    });
  });
});
