// Per-bucket "planning adjustments" overlaid on the historical budget snapshot, persisted in the
// URL so a tweaked view (rent hidden, health insurance set to a known going-forward figure) is
// bookmarkable. Pure + DOM-free so the encoding and the adjusted-total math are unit-testable;
// budget.tsx owns reading/writing window.location.
//
// A bucket is in exactly one of three states: actual (default, absent from the map), hidden, or
// overridden with a fixed monthly magnitude. An override is entered as a positive number and
// re-signed into the bucket's natural direction (Plaid convention: + out, - in) so the existing
// total/rollup math keeps working unchanged.

export type Adjustment = { kind: "hidden" } | { kind: "override"; monthly: number };

// bucketId -> adjustment. Buckets in their default "actual" state are simply absent.
export type Adjustments = Map<string, Adjustment>;

const HIDE_PARAM = "bhide";
const OVERRIDE_PARAM = "bset";

function splitList(value: string | null): string[] {
  return value ? value.split(",").filter((entry) => entry.length > 0) : [];
}

// `?bhide=rent,utilities` + `?bset=insurance:450,phone:0`. Bucket ids match ^[a-z0-9][a-z0-9_]*$,
// so neither "," nor ":" can occur inside one. Malformed override entries are skipped (augur is
// pre-production; we don't migrate old URL encodings).
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
    const raw = entry.slice(separator + 1);
    const monthly = Number(raw);
    if (raw !== "" && Number.isFinite(monthly) && monthly >= 0) {
      adjustments.set(id, { kind: "override", monthly });
    }
  }
  return adjustments;
}

// Serialize back to the two param strings (null when empty so the caller drops the key). Ids are
// sorted so the URL is stable regardless of the map's insertion order.
export function adjustmentsToParams(adjustments: Adjustments): { bhide: string | null; bset: string | null } {
  const hidden: string[] = [];
  const overrides: string[] = [];
  for (const [id, adjustment] of adjustments) {
    if (adjustment.kind === "hidden") hidden.push(id);
    else overrides.push(`${id}:${adjustment.monthly}`);
  }
  hidden.sort();
  overrides.sort();
  return {
    bhide: hidden.length ? hidden.join(",") : null,
    bset: overrides.length ? overrides.join(",") : null,
  };
}

// The signed monthly value to feed the existing total/rollup math. Hidden buckets keep their
// historical value here; callers exclude them separately. An override is entered as a positive
// magnitude and re-signed into the bucket's natural direction: inflows/income are money in (-);
// transfers are direction-agnostic so preserve the historical sign (negative = net inflow);
// expenses (and zero-history transfers) are outflows (+).
export function effectiveSignedAvg(kind: string, windowAvg: number, adjustment: Adjustment | undefined): number {
  if (adjustment?.kind !== "override") return windowAvg;
  if (kind === "inflow" || kind === "income") return -adjustment.monthly;
  if (kind === "transfer" && windowAvg < 0) return -adjustment.monthly;
  return adjustment.monthly;
}

export interface AdjustableRow {
  bucketId: string;
  kind: string;
  windowAvg: number;
}

export interface BudgetTotals {
  spend: number;
  inflow: number;
  income: number;
  netBurn: number;
}

// Headline totals after adjustments: hidden buckets drop out entirely; overrides replace the
// historical average. Covers the kinds the headline cares about (expense / inflow / income;
// transfers are surfaced per-family only). With an empty map this reduces to the plain
// window-average totals.
export function computeTotals(rows: readonly AdjustableRow[], adjustments: Adjustments): BudgetTotals {
  let spend = 0;
  let inflow = 0;
  let income = 0;
  for (const row of rows) {
    const adjustment = adjustments.get(row.bucketId);
    if (adjustment?.kind === "hidden") continue;
    const effective = effectiveSignedAvg(row.kind, row.windowAvg, adjustment);
    if (row.kind === "expense") spend += effective;
    else if (row.kind === "inflow") inflow += Math.abs(effective);
    else if (row.kind === "income") income += Math.abs(effective);
  }
  return { spend, inflow, income, netBurn: spend - inflow - income };
}
