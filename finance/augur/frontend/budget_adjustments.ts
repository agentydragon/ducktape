// Per-bucket "planning adjustments" overlaid on the historical budget snapshot, persisted in the
// URL so a tweaked view (rent hidden, health insurance set to a known going-forward figure) is
// bookmarkable. Pure + DOM-free so the encoding and the adjusted-total math are unit-testable;
// budget.tsx owns reading/writing window.location.

import Decimal from "decimal.js";
import type { BudgetSummaryCsvRequest } from "./client";

// Preserve the backend's finite Decimal precision through budget rollups and rate-independent
// planning arithmetic; the library default of 20 significant digits is too small for the wire.
Decimal.set({ precision: 80 });

// A bucket is in exactly one of three states: actual (default, absent from the map), hidden, or
// overridden with a fixed monthly magnitude. An override is entered as a positive decimal and
// re-signed into the bucket's natural direction (Plaid convention: + out, - in).
export type Adjustment = { kind: "hidden" } | { kind: "override"; monthly: Decimal };

// bucketId -> adjustment. Buckets in their default "actual" state are simply absent.
export type Adjustments = Map<string, Adjustment>;

export interface AdjustableRow {
  bucketId: string;
  kind: string;
  windowAvg: Decimal;
}

export interface BudgetTotals {
  spend: Decimal;
  inflow: Decimal;
  income: Decimal;
  netBurn: Decimal;
}

const HIDE_PARAM = "bhide";
const OVERRIDE_PARAM = "bset";
const DECIMAL_AMOUNT = /^\+?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

function splitList(value: string | null): string[] {
  return value ? value.split(",").filter((entry) => entry.length > 0) : [];
}

/** Parse a nonnegative decimal amount without passing through JavaScript Number. */
export function parseMonthlyAmount(raw: string): Decimal | null {
  if (!DECIMAL_AMOUNT.test(raw)) return null;
  try {
    const monthly = new Decimal(raw);
    return monthly.isFinite() && !monthly.isNegative() ? monthly : null;
  } catch {
    return null;
  }
}

// `?bhide=rent,utilities` + `?bset=insurance:450.25,phone:0`. Bucket ids match
// ^[a-z0-9][a-z0-9_]*$, so neither "," nor ":" can occur inside one. Malformed override entries
// are skipped (augur is pre-production; we don't migrate old URL encodings).
export function parseAdjustments(search: string): Adjustments {
  const params = new URLSearchParams(search);
  const adjustments: Adjustments = new Map();
  for (const id of splitList(params.get(HIDE_PARAM))) {
    adjustments.set(id, { kind: "hidden" });
  }
  for (const entry of splitList(params.get(OVERRIDE_PARAM))) {
    const separator = entry.indexOf(":");
    if (separator <= 0) continue;
    const id = entry.slice(0, separator);
    const monthly = parseMonthlyAmount(entry.slice(separator + 1));
    if (monthly != null) adjustments.set(id, { kind: "override", monthly });
  }
  return adjustments;
}

// Serialize back to the two param strings (null when empty so the caller drops the key). Ids are
// sorted so the URL is stable regardless of the map's insertion order. Decimal#toString preserves
// the exact value and is also what the JSON wire request needs for Decimal fields.
export function adjustmentsToParams(adjustments: Adjustments): { bhide: string | null; bset: string | null } {
  const hidden: string[] = [];
  const overrides: string[] = [];
  for (const [id, adjustment] of adjustments) {
    if (adjustment.kind === "hidden") hidden.push(id);
    else overrides.push(`${id}:${adjustment.monthly.toString()}`);
  }
  hidden.sort();
  overrides.sort();
  return {
    bhide: hidden.length ? hidden.join(",") : null,
    bset: overrides.length ? overrides.join(",") : null,
  };
}

/** Convert local Decimal adjustments to the generated string-valued request shape. */
export function adjustmentsToRequest(adjustments: Adjustments): BudgetSummaryCsvRequest["adjustments"] {
  const request: BudgetSummaryCsvRequest["adjustments"] = {};
  for (const [id, adjustment] of adjustments) {
    request[id] =
      adjustment.kind === "hidden" ? adjustment : { kind: "override", monthly: adjustment.monthly.toString() };
  }
  return request;
}

// The signed monthly value to feed the existing total/rollup math. Hidden buckets keep their
// historical value here; callers exclude them separately. An override is entered as a positive
// magnitude and re-signed into the bucket's natural direction: inflows/income are money in (-);
// transfers are direction-agnostic so preserve the historical sign (negative = net inflow);
// expenses (and zero-history transfers) are outflows (+).
export function effectiveSignedAvg(kind: string, windowAvg: Decimal, adjustment: Adjustment | undefined): Decimal {
  if (adjustment?.kind !== "override") return windowAvg;
  if (kind === "inflow" || kind === "income") return adjustment.monthly.negated();
  if (kind === "transfer" && windowAvg.lessThan(0)) return adjustment.monthly.negated();
  return adjustment.monthly;
}

// Headline totals after adjustments: hidden buckets drop out entirely; overrides replace the
// historical average. Covers the kinds the headline cares about (expense / inflow / income;
// transfers are surfaced per-family only). With an empty map this reduces to the plain
// window-average totals.
export function computeTotals(rows: readonly AdjustableRow[], adjustments: Adjustments): BudgetTotals {
  let spend = new Decimal(0);
  let inflow = new Decimal(0);
  let income = new Decimal(0);
  for (const row of rows) {
    const adjustment = adjustments.get(row.bucketId);
    if (adjustment?.kind === "hidden") continue;
    const effective = effectiveSignedAvg(row.kind, row.windowAvg, adjustment);
    if (row.kind === "expense") spend = spend.plus(effective);
    else if (row.kind === "inflow") inflow = inflow.plus(effective.abs());
    else if (row.kind === "income") income = income.plus(effective.abs());
  }
  return { spend, inflow, income, netBurn: spend.minus(inflow).minus(income) };
}
