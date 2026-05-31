// Pure presentation logic for the calibration page's "Reasonableness bands" panel.
//
// These are the deployment's hardcoded `sample_sanity` reasonableness bands (see
// `augur.model.sample_sanity`) evaluated against the calibration run's own rollouts: an
// expected range vs the observed value(s), same shape as the model-vs-market table but checked
// against this run. Kept React-free so the sort/summary/format logic is unit-testable.

import { fmtPct } from "./lib/format.js";

// `kind`s whose `observed`/`expected` numbers are probabilities in [0,1]; everything else is a
// unitless ratio (percentile_range/percentile_bound/anchor on a ratio check) or a count
// (count_range), which we render as a trimmed fixed-decimal number rather than a percentage.
const PROBABILITY_KINDS = new Set(["threshold_probability", "event_kind_probability"]);

// Loudest-first: failures, then skipped, then passes — mirrors `CleanTable`'s "loudest
// disagreement first" ordering. Stable within a group (input order preserved).
const STATUS_RANK = { fail: 0, skipped: 1, pass: 2 };

function statusRank(status) {
  return STATUS_RANK[status] ?? STATUS_RANK.pass;
}

export function sortSanityBands(bands) {
  return bands
    .map((band, index) => ({ band, index }))
    .sort((a, b) => statusRank(a.band.status) - statusRank(b.band.status) || a.index - b.index)
    .map((entry) => entry.band);
}

// "{n_pass}/{n_total} in band" is computed here, not read from the wire (no count field exists).
export function sanityPassCount(bands) {
  return bands.filter((band) => band.status === "pass").length;
}

// A single observed/expected value: probability kinds → `fmtPct`; everything else → a fixed
// 3-decimal number with trailing zeros trimmed (so 1.0 → "1", 0.25 → "0.25", 16.700 → "16.7").
export function fmtBandValue(kind, value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  if (PROBABILITY_KINDS.has(kind)) return fmtPct(value);
  return Number(Number(value).toFixed(3)).toString();
}

// The expected band: "[lo, hi]" when both sides are set, "≥ lo" / "≤ hi" for a one-sided bound,
// "—" when neither (e.g. an anchor or codes-allowed check carries no numeric range).
export function fmtExpectedBand(kind, band) {
  const hasLower = band.expectedLower != null && Number.isFinite(Number(band.expectedLower));
  const hasUpper = band.expectedUpper != null && Number.isFinite(Number(band.expectedUpper));
  if (hasLower && hasUpper)
    return `[${fmtBandValue(kind, band.expectedLower)}, ${fmtBandValue(kind, band.expectedUpper)}]`;
  if (hasLower) return `≥ ${fmtBandValue(kind, band.expectedLower)}`;
  if (hasUpper) return `≤ ${fmtBandValue(kind, band.expectedUpper)}`;
  return "—";
}

// The observed value(s) paired with their labels, e.g. "p1 0.25 · p99 16.7" or "p50 0.42".
// Falls back to "—" when the band carries no observed values.
export function fmtObserved(band) {
  const values = band.observed ?? [];
  if (values.length === 0) return "—";
  const labels = band.observedLabels ?? [];
  return values
    .map((value, index) => `${labels[index] ? `${labels[index]} ` : ""}${fmtBandValue(band.kind, value)}`)
    .join(" · ");
}
