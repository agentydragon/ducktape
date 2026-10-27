/**
 * Holds this package's formatters to the Python ones they are a port of.
 *
 * `aiquota/render/format.py` and `aiquota/pace.py` are canonical. Both JavaScript surfaces
 * consume `aiquota/shared/presentation.js`; these cases hold its browser adapters to Python's
 * output for the shared scenarios. The GNOME render test also loads the packaged copy.
 */

import { describe, expect, it } from "vitest";

import { displayUsedPercent, formatWindowLabel } from "./format";
import cases from "./fixtures/format_cases.json";
import {
  computePace,
  formatDuration,
  formatPace,
  formatPaceForecast,
  formatPrepaidBalance,
  isExhausted,
  roundHalfToEven,
  tintFor,
} from "../shared/presentation.js";

type FormatCase = {
  name: string | null;
  used_percent: number;
  reset_seconds: number;
  window_seconds: number;
  is_short: boolean;
  expected: {
    label: string;
    used_percent: number;
    reset: string;
    pace: string | null;
    forecast: string | null;
    tint: string;
  };
};

const CASES = cases as FormatCase[];

describe("the Python renderers' output", () => {
  // Without this the loop below silently becomes zero tests if the generator emits nothing.
  it("is generated for the scenarios at all", () => {
    expect(CASES.length).toBeGreaterThan(0);
  });

  for (const { name, used_percent, reset_seconds, window_seconds, is_short, expected } of CASES) {
    const span = `${window_seconds}s window, ${used_percent}% used, ${reset_seconds}s to reset`;
    it(`is reproduced for ${is_short ? "short" : "long"} ${span}`, () => {
      const window = { usedPercent: used_percent, resetSeconds: reset_seconds, windowSeconds: window_seconds };
      const pace = isExhausted(window) ? null : computePace(window);
      expect({
        label: formatWindowLabel({ name, window_seconds }),
        used_percent: displayUsedPercent({ used_percent }),
        reset: formatDuration(reset_seconds),
        pace: formatPace(pace),
        forecast: formatPaceForecast(pace, reset_seconds),
        tint: tintFor(pace, used_percent, { isShort: is_short }),
      }).toEqual(expected);
    });
  }
});

describe("formatWindowLabel", () => {
  // No scenario names its windows, so the named form has no generated case above; Codex's
  // real response does name them ("primary").
  it("qualifies a named window with its length", () => {
    expect(formatWindowLabel({ name: "primary", window_seconds: 3600 })).toBe("primary (1h)");
  });
});

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
    expect(displayUsedPercent({ used_percent: 12.5 })).toBe(12);
    expect(formatPace({ deviation: 12.5, projectedAtReset: 75, secondsToExhaust: null, stable: true })).toBe("+12%");
    expect(formatDuration(3598.5)).toBe("59m");
  });
});
