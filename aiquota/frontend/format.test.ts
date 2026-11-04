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
  formatDeadline,
  formatResetCreditSummary,
  formatPrepaidBalance,
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
      "2 · next known expiry Jan 1, 15:20 UTC"
    );
    expect(formatResetCreditSummary(2, [firstExpiry, secondExpiry], false, now, true)).toBe(
      "2 · known expiries 2026-01-01 15:20 UTC, 2026-02-02 12:34 UTC"
    );
  });

  it("shows the difference between the count and a successful partial expiry lookup", () => {
    expect(formatResetCreditSummary(2, [firstExpiry], false, now)).toBe(
      "2 · next known expiry Jan 1, 15:20 UTC · 1 expiry unknown"
    );
    expect(formatResetCreditSummary(1, [firstExpiry], false, now)).toBe("1 · next known expiry Jan 1, 15:20 UTC");
    expect(formatResetCreditSummary(2, [], false, now)).toBe("2 · 2 expiries unknown");
  });

  it("does not infer an unknown count when the expiry lookup fails", () => {
    expect(formatResetCreditSummary(2, [], true, now)).toBe("2 · expiry lookup failed");
    expect(formatResetCreditSummary(2, [firstExpiry], true, now)).toContain("expiry lookup failed");
  });

  it("keeps a zero reset count concise when the lookup succeeds", () => {
    expect(formatResetCreditSummary(0, [], false, now)).toBe("0");
  });
});

describe("formatDeadline", () => {
  const now = new Date("2026-01-15T12:00:00Z");

  it("uses a local date, includes the year when needed, and keeps nearby minute precision", () => {
    expect(formatDeadline("2026-02-02T12:34:00Z", now)).toBe("Feb 2");
    expect(formatDeadline("2027-02-02T12:34:00Z", now)).toBe("Feb 2, 2027");
    expect(formatDeadline("2026-01-15T12:34:00Z", now)).toBe("Jan 15, 12:34 UTC");
  });

  it("uses the 14-day cutoff and gives grant details the full local timestamp", () => {
    expect(formatDeadline("2026-01-29T11:59:59Z", now)).toBe("Jan 29, 11:59 UTC");
    expect(formatDeadline("2026-01-29T12:00:00Z", now)).toBe("Jan 29, 12:00 UTC");
    expect(formatDeadline("2026-01-29T12:00:01Z", now)).toBe("Jan 29");
    expect(formatDeadline("2026-02-02T12:34:00Z", now, true)).toBe("2026-02-02 12:34 UTC");
  });

  it("returns null for an invalid deadline", () => {
    expect(formatDeadline("not a timestamp", now)).toBeNull();
  });
});
