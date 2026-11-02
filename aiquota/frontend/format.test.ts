/**
 * Holds this package's formatters to the Python ones they are a port of.
 *
 * `aiquota/render/format.py` and `aiquota/pace.py` are canonical. Both JavaScript surfaces
 * consume `aiquota/shared/presentation.js`; these cases hold its browser adapters to Python's
 * output for the shared scenarios. The GNOME render test also loads the packaged copy.
 */

import { describe, expect, it } from "vitest";

import {
  formatDuration,
  formatKnownExpiry,
  formatPrepaidBalance,
  formatResetCreditExpiry,
  formatResetCreditSummary,
  roundHalfToEven,
} from "../shared/presentation.js";

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

  it("lists all known expiries", () => {
    expect(formatResetCreditSummary(2, [firstExpiry, secondExpiry], false, now)).toBe(
      `2 resets (expiry: ${formatKnownExpiry(firstExpiry)}, ${formatResetCreditExpiry(secondExpiry, now)})`
    );
  });

  it("shows the difference between the count and a successful partial expiry lookup", () => {
    expect(formatResetCreditSummary(2, [firstExpiry], false, now)).toBe(
      `2 resets (expiry: ${formatKnownExpiry(firstExpiry)}, 1 unknown)`
    );
    expect(formatResetCreditSummary(1, [firstExpiry], false, now)).toBe(
      `1 reset (expiry: ${formatKnownExpiry(firstExpiry)})`
    );
    expect(formatResetCreditSummary(2, [], false, now)).toBe("2 resets (expiry: 2 unknown)");
  });

  it("does not infer an unknown count when the expiry lookup fails", () => {
    expect(formatResetCreditSummary(2, [], true, now)).toBe("2 resets (expiry lookup failed)");
    expect(formatResetCreditSummary(2, [firstExpiry], true, now)).toBe(
      `2 resets (expiry: ${formatKnownExpiry(firstExpiry)}; lookup failed)`
    );
  });

  it("keeps a zero reset count concise when the lookup succeeds", () => {
    expect(formatResetCreditSummary(0, [], false, now)).toBe("0 resets");
  });

  it.each([
    ["2026-01-29T11:59:59Z", false],
    ["2026-01-29T12:00:00Z", false],
    ["2026-01-29T12:00:01Z", true],
    ["2026-01-14T09:00:00Z", false],
  ])("formats %s with a date-only value only when it is over two weeks away", (iso, isDateOnly) => {
    const expiry = new Date(iso);
    const rendered = formatResetCreditExpiry(expiry, now);
    expect(rendered).toBe(
      isDateOnly ? expiry.toLocaleDateString([], { month: "short", day: "numeric" }) : formatKnownExpiry(expiry)
    );
  });
});
