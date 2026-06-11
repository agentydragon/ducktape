// The deployment's hardcoded `sample_sanity` reasonableness bands evaluated against THIS run's
// rollouts (same expected-range-vs-observed shape as the model-vs-market table, but checked
// against the run's own rollouts). Renders nothing when no bands are configured.

import React, { useMemo } from "react";

import { sortSanityBands, sanityPassCount, fmtExpectedBand, fmtObserved } from "./sanity_bands.ts";

// Reasonableness-band status → pill classes. A failing band reads loudest (rose), a passing
// band reassuring (emerald), a skipped band muted (slate), an unmodeled band a distinct amber
// (the spec asked for a series the preset can't emit — a config-shape signal, not a model
// reading) — the same rose/amber/muted family the KL tints draw from.
const SANITY_PILL_CLASS = {
  pass: "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-400/20",
  fail: "bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-950/40 dark:text-rose-300 dark:ring-rose-400/20",
  skipped: "bg-slate-100 text-slate-600 ring-slate-500/20 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-400/20",
  unmodeled:
    "bg-amber-50 text-amber-700 ring-amber-600/20 dark:bg-amber-950/40 dark:text-amber-300 dark:ring-amber-400/20",
};

function SanityStatusPill({ status }) {
  const tone = SANITY_PILL_CLASS[status] ?? SANITY_PILL_CLASS.skipped;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ring-1 ring-inset ${tone}`}
    >
      {status}
    </span>
  );
}

export function SanityBandsPanel({ bands }) {
  const sorted = useMemo(() => sortSanityBands(bands), [bands]);
  const passing = sanityPassCount(bands);
  return (
    <section className="augur-panel overflow-hidden" aria-label="Reasonableness bands" data-calibration-sanity-panel="">
      <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="min-w-0">
          <div className="augur-eyebrow">Reasonableness bands (deploy gate)</div>
          <div className="mt-1 text-xs augur-muted">
            The hardcoded <code>sample_sanity</code> reasonableness bands: an expected range vs the observed value, the
            same shape as the model-vs-market calibration above but checked against this run&apos;s own rollouts. Tail
            percentile bands (p1/p99) are noisier at this page&apos;s rollout count than at the deploy gate&apos;s
            higher count.
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="augur-tabular text-sm font-semibold augur-strong">
            {passing}/{bands.length}
          </div>
          <div className="text-[11px] augur-muted">in band</div>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full border-t border-slate-200 text-sm dark:border-slate-700">
          <thead>
            <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
              <th className="px-4 py-2 font-semibold">Check</th>
              <th className="px-3 py-2 text-right font-semibold">Expected</th>
              <th className="px-3 py-2 text-right font-semibold">Observed</th>
              <th className="px-3 py-2 text-right font-semibold">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {sorted.map((band) => (
              <tr
                key={band.label}
                data-calibration-sanity-row={band.label}
                data-calibration-sanity-status={band.status}
              >
                <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                  {band.label}
                  {band.status !== "pass" && band.detail && (
                    <div className="mt-0.5 text-xs font-normal augur-muted">{band.detail}</div>
                  )}
                </th>
                <td className="px-3 py-2 text-right align-top augur-tabular">{fmtExpectedBand(band.kind, band)}</td>
                <td className="px-3 py-2 text-right align-top augur-tabular">{fmtObserved(band)}</td>
                <td className="px-3 py-2 text-right align-top">
                  <SanityStatusPill status={band.status} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
