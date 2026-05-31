// Unit tests for the calibration "Reasonableness bands" panel's pure presentation logic.
// Uses the Node.js built-in test runner (no extra dependencies), mirroring
// props/frontend/tests/router.test.mjs.

import { test } from "node:test";
import assert from "node:assert/strict";

import { sortSanityBands, sanityPassCount, fmtBandValue, fmtExpectedBand, fmtObserved } from "./sanity_bands.js";

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

test("sortSanityBands: failures first, then skipped, then passes", () => {
  const input = [
    band({ label: "pass-a", status: "pass" }),
    band({ label: "skip-a", status: "skipped" }),
    band({ label: "fail-a", status: "fail" }),
    band({ label: "pass-b", status: "pass" }),
    band({ label: "fail-b", status: "fail" }),
  ];
  assert.deepEqual(
    sortSanityBands(input).map((b) => b.label),
    ["fail-a", "fail-b", "skip-a", "pass-a", "pass-b"]
  );
});

test("sortSanityBands: stable within a status group (input order preserved)", () => {
  const input = [
    band({ label: "fail-2", status: "fail" }),
    band({ label: "fail-1", status: "fail" }),
    band({ label: "fail-3", status: "fail" }),
  ];
  assert.deepEqual(
    sortSanityBands(input).map((b) => b.label),
    ["fail-2", "fail-1", "fail-3"]
  );
  // The input array is not mutated.
  assert.deepEqual(
    input.map((b) => b.label),
    ["fail-2", "fail-1", "fail-3"]
  );
});

test("sanityPassCount: counts only passes, computed from the array", () => {
  assert.equal(sanityPassCount([]), 0);
  assert.equal(
    sanityPassCount([
      band({ status: "pass" }),
      band({ status: "fail" }),
      band({ status: "pass" }),
      band({ status: "skipped" }),
    ]),
    2
  );
});

test("fmtBandValue: probability kinds format as percent", () => {
  assert.equal(fmtBandValue("threshold_probability", 0.42), "42.0%");
  assert.equal(fmtBandValue("event_kind_probability", 0.015), "1.5%");
});

test("fmtBandValue: non-probability kinds format as trimmed fixed-decimal ratios/counts", () => {
  assert.equal(fmtBandValue("percentile_range", 1.0), "1");
  assert.equal(fmtBandValue("percentile_range", 0.25), "0.25");
  assert.equal(fmtBandValue("percentile_bound", 16.7), "16.7");
  assert.equal(fmtBandValue("count_range", 12), "12");
  assert.equal(fmtBandValue("anchor", 100.0), "100");
});

test("fmtBandValue: null/undefined/non-finite render as em dash", () => {
  assert.equal(fmtBandValue("percentile_range", null), "—");
  assert.equal(fmtBandValue("percentile_range", undefined), "—");
  assert.equal(fmtBandValue("percentile_range", NaN), "—");
});

test("fmtExpectedBand: two-sided, one-sided, and absent bounds", () => {
  assert.equal(fmtExpectedBand("percentile_range", band({ expectedLower: 0.2, expectedUpper: 20 })), "[0.2, 20]");
  assert.equal(fmtExpectedBand("percentile_bound", band({ expectedLower: 0.2 })), "≥ 0.2");
  assert.equal(fmtExpectedBand("percentile_bound", band({ expectedUpper: 20 })), "≤ 20");
  assert.equal(fmtExpectedBand("finite", band({})), "—");
});

test("fmtExpectedBand: probability bound formats endpoints as percent", () => {
  assert.equal(
    fmtExpectedBand(
      "threshold_probability",
      band({ kind: "threshold_probability", expectedLower: 0.1, expectedUpper: 0.9 })
    ),
    "[10.0%, 90.0%]"
  );
});

test("fmtObserved: pairs values with labels", () => {
  assert.equal(fmtObserved(band({ observed: [0.25, 16.7], observedLabels: ["p1", "p99"] })), "p1 0.25 · p99 16.7");
  assert.equal(fmtObserved(band({ observed: [0.42], observedLabels: ["p50"] })), "p50 0.42");
});

test("fmtObserved: probability kind observed values render as percent", () => {
  assert.equal(
    fmtObserved(band({ kind: "threshold_probability", observed: [0.42], observedLabels: ["probability"] })),
    "probability 42.0%"
  );
});

test("fmtObserved: missing labels and empty observed degrade gracefully", () => {
  assert.equal(fmtObserved(band({ observed: [0.5], observedLabels: [] })), "0.5");
  assert.equal(fmtObserved(band({ observed: [] })), "—");
});
