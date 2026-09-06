/**
 * Holds this package's formatters to the Python ones they are a port of.
 *
 * `aiquota/render/format.py` and `aiquota/pace.py` are canonical: the CLI and the GNOME popup
 * render from them, and the dashboard is read beside both. The cases are generated from the
 * shared scenarios by `//aiquota/testing:export_web_fixtures_bin`, which records what Python
 * produces for every window in them — so editing one side alone fails here rather than
 * quietly showing a different forecast in the browser than in the terminal.
 */

import { describe, expect, it } from "vitest";

import { displayUsedPercent, formatDuration, formatPace, formatPaceForecast, formatWindowLabel } from "./format";
import cases from "./fixtures/format_cases.json";
import { computePace, isExhausted, tintFor } from "./pace";

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
