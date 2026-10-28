// Unit tests for the budget tab's planning-adjustment encoding + adjusted-total math.
// Runs under vitest; see //augur/frontend:budget_adjustments_test.

import { test, expect } from "vitest";
import Decimal from "decimal.js";

import {
  type Adjustments,
  parseAdjustments,
  adjustmentsToParams,
  adjustmentsToRequest,
  effectiveSignedAvg,
  computeTotals,
  parseMonthlyAmount,
} from "./budget_adjustments";

test("parseAdjustments reads hidden buckets and numeric overrides", () => {
  const adjustments = parseAdjustments("?bhide=rent,utilities&bset=insurance:450.25,phone:0");
  expect(adjustments.get("rent")).toEqual({ kind: "hidden" });
  expect(adjustments.get("utilities")).toEqual({ kind: "hidden" });
  expect(adjustments.get("insurance")).toEqual({ kind: "override", monthly: new Decimal("450.25") });
  // A zero override is meaningful ("I'll stop paying this") and distinct from hiding.
  expect(adjustments.get("phone")).toEqual({ kind: "override", monthly: new Decimal("0") });
});

test("parseAdjustments skips malformed or negative override entries", () => {
  const adjustments = parseAdjustments("?bset=insurance:,broken,neg:-5,ok:12");
  expect(adjustments.has("insurance")).toBe(false); // empty value
  expect(adjustments.has("broken")).toBe(false); // no colon
  expect(adjustments.has("neg")).toBe(false); // negative rejected
  expect(adjustments.get("ok")).toEqual({ kind: "override", monthly: new Decimal("12") });
});

test("monthly amounts stay exact beyond JavaScript's safe integer range", () => {
  const adjustments = parseAdjustments("?bset=rent:9007199254740993.125");
  const rent = adjustments.get("rent");
  expect(rent?.kind).toBe("override");
  if (rent?.kind !== "override") throw new Error("rent override was not parsed");
  expect(rent.monthly.toString()).toBe("9007199254740993.125");
  expect(adjustmentsToParams(adjustments).bset).toBe("rent:9007199254740993.125");
  expect(adjustmentsToRequest(adjustments)).toEqual({
    rent: { kind: "override", monthly: "9007199254740993.125" },
  });
  const totals = computeTotals(
    [
      { bucketId: "rent", kind: "expense", windowAvg: new Decimal("1") },
      { bucketId: "utilities", kind: "expense", windowAvg: new Decimal("0.25") },
    ],
    adjustments
  );
  expect(totals.spend.toString()).toBe("9007199254740993.375");
  expect(totals.netBurn.toString()).toBe("9007199254740993.375");
  expect(parseMonthlyAmount("0x10")).toBeNull();
  expect(parseMonthlyAmount("Infinity")).toBeNull();
});

test("adjustmentsToParams round-trips and sorts ids for stable URLs", () => {
  const adjustments: Adjustments = new Map([
    ["utilities", { kind: "hidden" }],
    ["rent", { kind: "hidden" }],
    ["insurance", { kind: "override", monthly: new Decimal("450.125") }],
  ]);
  const params = adjustmentsToParams(adjustments);
  expect(params.bhide).toBe("rent,utilities");
  expect(params.bset).toBe("insurance:450.125");
  // Round-trip through a query string reproduces the same map.
  const search = `?bhide=${params.bhide}&bset=${params.bset}`;
  expect(parseAdjustments(search)).toEqual(adjustments);
});

test("adjustmentsToParams returns null for empty sides", () => {
  expect(adjustmentsToParams(new Map())).toEqual({ bhide: null, bset: null });
});

test("effectiveSignedAvg re-signs an override into the bucket's natural direction", () => {
  expect(
    effectiveSignedAvg("expense", new Decimal("312"), { kind: "override", monthly: new Decimal("450.25") }).toString()
  ).toBe("450.25");
  // Inflows/income are stored negative (money in); an override magnitude re-signs negative.
  expect(
    effectiveSignedAvg("inflow", new Decimal("-300"), { kind: "override", monthly: new Decimal("280.25") }).toString()
  ).toBe("-280.25");
  // No override (or hidden) keeps the historical value.
  expect(effectiveSignedAvg("expense", new Decimal("312"), undefined).toString()).toBe("312");
  expect(effectiveSignedAvg("expense", new Decimal("312"), { kind: "hidden" }).toString()).toBe("312");
});

test("effectiveSignedAvg preserves a transfer's historical direction", () => {
  // Transfers are direction-agnostic: an override keeps pointing the way the history did.
  expect(
    effectiveSignedAvg("transfer", new Decimal("-1000"), {
      kind: "override",
      monthly: new Decimal("800.125"),
    }).toString()
  ).toBe("-800.125");
  expect(
    effectiveSignedAvg("transfer", new Decimal("1000"), {
      kind: "override",
      monthly: new Decimal("800.125"),
    }).toString()
  ).toBe("800.125");
});

const ROWS = [
  { bucketId: "rent", kind: "expense", windowAvg: new Decimal("3200.10") },
  { bucketId: "groceries", kind: "expense", windowAvg: new Decimal("600.20") },
  { bucketId: "insurance", kind: "expense", windowAvg: new Decimal("312.30") },
  { bucketId: "reimbursement", kind: "inflow", windowAvg: new Decimal("-200.40") },
  { bucketId: "salary", kind: "income", windowAvg: new Decimal("-5000.50") },
];

test("computeTotals with no adjustments sums window averages", () => {
  const totals = computeTotals(ROWS, new Map());
  expect(totals.spend.toString()).toBe("4112.6");
  expect(totals.inflow.toString()).toBe("200.4");
  expect(totals.income.toString()).toBe("5000.5");
  expect(totals.netBurn.toString()).toBe("-1088.3");
});

test("computeTotals drops hidden buckets from spend and net burn", () => {
  const totals = computeTotals(ROWS, new Map([["rent", { kind: "hidden" }]]));
  expect(totals.spend.toString()).toBe("912.5");
  expect(totals.netBurn.toString()).toBe("-4288.4");
});

test("computeTotals applies an expense override in place of the historical average", () => {
  const totals = computeTotals(ROWS, new Map([["insurance", { kind: "override", monthly: new Decimal("450.125") }]]));
  expect(totals.spend.toString()).toBe("4250.425");
});

test("computeTotals applies an inflow override by magnitude", () => {
  const totals = computeTotals(
    ROWS,
    new Map([["reimbursement", { kind: "override", monthly: new Decimal("500.25") }]])
  );
  expect(totals.inflow.toString()).toBe("500.25");
});
