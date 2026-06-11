// Unit tests for the budget tab's planning-adjustment encoding + adjusted-total math.
// Runs under vitest; see //augur/frontend:budget_adjustments_test.

import { test, expect } from "vitest";

import {
  type Adjustments,
  parseAdjustments,
  adjustmentsToParams,
  effectiveSignedAvg,
  computeTotals,
} from "./budget_adjustments.ts";

test("parseAdjustments reads hidden buckets and numeric overrides", () => {
  const adjustments = parseAdjustments("?bhide=rent,utilities&bset=insurance:450,phone:0");
  expect(adjustments.get("rent")).toEqual({ kind: "hidden" });
  expect(adjustments.get("utilities")).toEqual({ kind: "hidden" });
  expect(adjustments.get("insurance")).toEqual({ kind: "override", monthly: 450 });
  // A zero override is meaningful ("I'll stop paying this") and distinct from hiding.
  expect(adjustments.get("phone")).toEqual({ kind: "override", monthly: 0 });
});

test("parseAdjustments skips malformed or negative override entries", () => {
  const adjustments = parseAdjustments("?bset=insurance:,broken,neg:-5,ok:12");
  expect(adjustments.has("insurance")).toBe(false); // empty value
  expect(adjustments.has("broken")).toBe(false); // no colon
  expect(adjustments.has("neg")).toBe(false); // negative rejected
  expect(adjustments.get("ok")).toEqual({ kind: "override", monthly: 12 });
});

test("adjustmentsToParams round-trips and sorts ids for stable URLs", () => {
  const adjustments: Adjustments = new Map([
    ["utilities", { kind: "hidden" }],
    ["rent", { kind: "hidden" }],
    ["insurance", { kind: "override", monthly: 450 }],
  ]);
  const params = adjustmentsToParams(adjustments);
  expect(params.bhide).toBe("rent,utilities");
  expect(params.bset).toBe("insurance:450");
  // Round-trip through a query string reproduces the same map.
  const search = `?bhide=${params.bhide}&bset=${params.bset}`;
  expect(parseAdjustments(search)).toEqual(adjustments);
});

test("adjustmentsToParams returns null for empty sides", () => {
  expect(adjustmentsToParams(new Map())).toEqual({ bhide: null, bset: null });
});

test("effectiveSignedAvg re-signs an override into the bucket's natural direction", () => {
  expect(effectiveSignedAvg("expense", 312, { kind: "override", monthly: 450 })).toBe(450);
  // Inflows/income are stored negative (money in); an override magnitude re-signs negative.
  expect(effectiveSignedAvg("inflow", -300, { kind: "override", monthly: 280 })).toBe(-280);
  // No override (or hidden) keeps the historical value.
  expect(effectiveSignedAvg("expense", 312, undefined)).toBe(312);
  expect(effectiveSignedAvg("expense", 312, { kind: "hidden" })).toBe(312);
});

test("effectiveSignedAvg preserves a transfer's historical direction", () => {
  // Transfers are direction-agnostic: an override keeps pointing the way the history did.
  expect(effectiveSignedAvg("transfer", -1000, { kind: "override", monthly: 800 })).toBe(-800);
  expect(effectiveSignedAvg("transfer", 1000, { kind: "override", monthly: 800 })).toBe(800);
});

const ROWS = [
  { bucketId: "rent", kind: "expense", windowAvg: 3200 },
  { bucketId: "groceries", kind: "expense", windowAvg: 600 },
  { bucketId: "insurance", kind: "expense", windowAvg: 312 },
  { bucketId: "reimbursement", kind: "inflow", windowAvg: -200 },
  { bucketId: "salary", kind: "income", windowAvg: -5000 },
];

test("computeTotals with no adjustments sums window averages", () => {
  const totals = computeTotals(ROWS, new Map());
  expect(totals.spend).toBe(4112);
  expect(totals.inflow).toBe(200);
  expect(totals.income).toBe(5000);
  expect(totals.netBurn).toBe(4112 - 200 - 5000);
});

test("computeTotals drops hidden buckets from spend and net burn", () => {
  const totals = computeTotals(ROWS, new Map([["rent", { kind: "hidden" }]]));
  expect(totals.spend).toBe(912); // 4112 - 3200
  expect(totals.netBurn).toBe(912 - 200 - 5000);
});

test("computeTotals applies an expense override in place of the historical average", () => {
  const totals = computeTotals(ROWS, new Map([["insurance", { kind: "override", monthly: 450 }]]));
  expect(totals.spend).toBe(4250); // 4112 - 312 + 450
});

test("computeTotals applies an inflow override by magnitude", () => {
  const totals = computeTotals(ROWS, new Map([["reimbursement", { kind: "override", monthly: 500 }]]));
  expect(totals.inflow).toBe(500);
});
