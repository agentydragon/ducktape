// Unit tests for the calibration "Reasonableness bands" panel's pure presentation logic.
// Runs under vitest (the repo's TS unit-test runner); see //augur/frontend:sanity_bands_test.

import { test, expect } from "vitest";

import { sortSanityBands, sanityPassCount, fmtBandValue, fmtExpectedBand, fmtObserved } from "./sanity_bands";

function band(overrides) {
  return {
    label: "band",
    seriesId: "sp500",
    kind: "percentile_range",
    month: null,
    expectedLower: null,
    expectedUpper: null,
    observed: [],
    observedLabels: [],
    status: "pass",
    detail: "",
    ...overrides,
  };
}

test("sortSanityBands: failures first, then unmodeled, then skipped, then passes", () => {
  const input = [
    band({ label: "pass-a", status: "pass" }),
    band({ label: "skip-a", status: "skipped" }),
    band({ label: "unmodeled-a", status: "unmodeled" }),
    band({ label: "fail-a", status: "fail" }),
    band({ label: "pass-b", status: "pass" }),
    band({ label: "fail-b", status: "fail" }),
  ];
  expect(sortSanityBands(input).map((b) => b.label)).toEqual([
    "fail-a",
    "fail-b",
    "unmodeled-a",
    "skip-a",
    "pass-a",
    "pass-b",
  ]);
});

test("sortSanityBands: stable within a status group (input order preserved)", () => {
  const input = [
    band({ label: "fail-2", status: "fail" }),
    band({ label: "fail-1", status: "fail" }),
    band({ label: "fail-3", status: "fail" }),
  ];
  expect(sortSanityBands(input).map((b) => b.label)).toEqual(["fail-2", "fail-1", "fail-3"]);
  // The input array is not mutated.
  expect(input.map((b) => b.label)).toEqual(["fail-2", "fail-1", "fail-3"]);
});

test("sanityPassCount: counts only passes, computed from the array", () => {
  expect(sanityPassCount([])).toBe(0);
  expect(
    sanityPassCount([
      band({ status: "pass" }),
      band({ status: "fail" }),
      band({ status: "pass" }),
      band({ status: "skipped" }),
    ])
  ).toBe(2);
});

test("fmtBandValue: probability kinds format as percent", () => {
  expect(fmtBandValue("threshold_probability", 0.42)).toBe("42.0%");
  expect(fmtBandValue("event_kind_probability", 0.015)).toBe("1.5%");
});

test("fmtBandValue: non-probability kinds format as trimmed fixed-decimal ratios/counts", () => {
  expect(fmtBandValue("percentile_range", 1.0)).toBe("1");
  expect(fmtBandValue("percentile_range", 0.25)).toBe("0.25");
  expect(fmtBandValue("percentile_bound", 16.7)).toBe("16.7");
  expect(fmtBandValue("count_range", 12)).toBe("12");
  expect(fmtBandValue("anchor", 100.0)).toBe("100");
});

test("fmtBandValue: null/undefined/non-finite render as em dash", () => {
  expect(fmtBandValue("percentile_range", null)).toBe("—");
  expect(fmtBandValue("percentile_range", undefined)).toBe("—");
  expect(fmtBandValue("percentile_range", NaN)).toBe("—");
});

test("fmtExpectedBand: two-sided, one-sided, and absent bounds", () => {
  expect(fmtExpectedBand("percentile_range", band({ expectedLower: 0.2, expectedUpper: 20 }))).toBe("[0.2, 20]");
  expect(fmtExpectedBand("percentile_bound", band({ expectedLower: 0.2 }))).toBe("≥ 0.2");
  expect(fmtExpectedBand("percentile_bound", band({ expectedUpper: 20 }))).toBe("≤ 20");
  expect(fmtExpectedBand("codes_allowed", band({}))).toBe("—");
});

test("fmtExpectedBand: probability bound formats endpoints as percent", () => {
  expect(
    fmtExpectedBand(
      "threshold_probability",
      band({ kind: "threshold_probability", expectedLower: 0.1, expectedUpper: 0.9 })
    )
  ).toBe("[10.0%, 90.0%]");
});

test("fmtObserved: pairs values with labels", () => {
  expect(fmtObserved(band({ observed: [0.25, 16.7], observedLabels: ["p1", "p99"] }))).toBe("p1 0.25 · p99 16.7");
  expect(fmtObserved(band({ observed: [0.42], observedLabels: ["p50"] }))).toBe("p50 0.42");
});

test("fmtObserved: probability kind observed values render as percent", () => {
  expect(fmtObserved(band({ kind: "threshold_probability", observed: [0.42], observedLabels: ["probability"] }))).toBe(
    "probability 42.0%"
  );
});

test("fmtObserved: missing labels and empty observed degrade gracefully", () => {
  expect(fmtObserved(band({ observed: [0.5], observedLabels: [] }))).toBe("0.5");
  expect(fmtObserved(band({ observed: [] }))).toBe("—");
});
