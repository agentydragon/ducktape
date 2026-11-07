// The two market tables on the calibration tab: scored ("clean", apples-to-apples model-vs-market
// with a KL column) and surfaced (context-only, market price beside a related augur signal).

import React from "react";

import { fmtProb, fmtKl, klTextClass, PlatformBadge } from "./calibration_format";
import { fmtPct, fmtVolume } from "./lib/format";
import type { fetchCalibrationRun } from "./client";

type CalibrationRun = Awaited<ReturnType<typeof fetchCalibrationRun>>;
type CleanRow = CalibrationRun["result"]["clean"][number];
type SurfacedRow = CalibrationRun["result"]["surfaced"][number];

export function CleanTable({ rows }: { rows: readonly CleanRow[] }) {
  if (rows.length === 0) {
    return <div className="px-4 py-6 text-sm augur-muted">No apples-to-apples (scored) markets in this catalog.</div>;
  }
  // Loudest disagreements first; rows the model couldn't resolve (no `kl_bits`) sink to the bottom.
  const sorted = rows.slice().sort((left, right) => (right.kl_bits ?? -Infinity) - (left.kl_bits ?? -Infinity));
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full border-t border-slate-200 text-sm dark:border-slate-700">
        <thead>
          <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            <th className="w-8 px-2 py-2 font-semibold" aria-label="Source platform" />
            <th className="px-4 py-2 font-semibold">Market</th>
            <th className="px-3 py-2 text-right font-semibold">Market</th>
            <th className="px-3 py-2 text-right font-semibold">Model (95% CI)</th>
            <th className="px-3 py-2 text-right font-semibold" title="D_KL(market ‖ model)">
              KL (bits)
            </th>
            <th className="px-3 py-2 text-right font-semibold">Unresolved</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {sorted.map((row) => {
            const ci = row.ci95 ?? [];
            const unresolvedPct =
              row.n_resolved + row.unresolved > 0 ? row.unresolved / (row.n_resolved + row.unresolved) : null;
            return (
              <tr key={row.market_id} data-calibration-clean-row={row.market_id}>
                <td className="px-2 py-2 text-center align-top">
                  <PlatformBadge platform={row.platform} />
                </td>
                <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                  <a href={row.url} target="_blank" rel="noreferrer" className="augur-accent-text hover:underline">
                    {row.question}
                  </a>
                  {row.channel && (
                    <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide augur-muted dark:bg-slate-800">
                      {row.channel}
                    </span>
                  )}
                  {fmtVolume(row.volume, row.volume_unit) && (
                    <div
                      className="mt-0.5 text-[11px] font-normal augur-muted augur-tabular"
                      title="total all-time volume traded on the platform"
                    >
                      {fmtVolume(row.volume, row.volume_unit)}
                    </div>
                  )}
                </th>
                <td className="px-3 py-2 text-right augur-tabular">{fmtProb(row.p_market)}</td>
                <td className="px-3 py-2 text-right augur-tabular">
                  {row.p_model == null ? (
                    <span className="augur-muted">n/a</span>
                  ) : (
                    <>
                      {fmtProb(row.p_model)}
                      {ci.length === 2 && (
                        <span className="ml-1 text-xs augur-muted">
                          [{fmtProb(ci[0])}–{fmtProb(ci[1])}]
                        </span>
                      )}
                    </>
                  )}
                </td>
                <td className={`px-3 py-2 text-right ${klTextClass(row.kl_bits)}`}>{fmtKl(row.kl_bits)}</td>
                <td className="px-3 py-2 text-right augur-tabular">
                  {unresolvedPct == null ? "—" : fmtPct(unresolvedPct)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function SurfacedTable({ rows }: { rows: readonly SurfacedRow[] }) {
  if (rows.length === 0) {
    return <div className="px-4 py-6 text-sm augur-muted">No surfaced (context-only) markets in this catalog.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full border-t border-slate-200 text-sm dark:border-slate-700">
        <thead>
          <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            <th className="w-8 px-2 py-2 font-semibold" aria-label="Source platform" />
            <th className="px-4 py-2 font-semibold">Market</th>
            <th className="px-3 py-2 text-right font-semibold">Market</th>
            <th className="px-3 py-2 text-right font-semibold">Augur signal</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {rows.map((row) => (
            <tr key={row.market_id} data-calibration-surfaced-row={row.market_id}>
              <td className="px-2 py-2 text-center align-top">
                <PlatformBadge platform={row.platform} />
              </td>
              <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                <a href={row.url} target="_blank" rel="noreferrer" className="augur-accent-text hover:underline">
                  {row.question}
                </a>
                <div className="mt-0.5 text-[11px] font-normal augur-muted">
                  <span className="font-semibold">
                    {row.correlate_of ? `correlate of ${row.correlate_of}` : row.mappability}
                  </span>
                  {fmtVolume(row.volume, row.volume_unit) && (
                    <span className="ml-2 augur-tabular" title="total all-time volume traded on the platform">
                      · {fmtVolume(row.volume, row.volume_unit)}
                    </span>
                  )}
                </div>
                {row.reason && <div className="mt-0.5 text-xs font-normal augur-body">{row.reason}</div>}
              </th>
              <td className="px-3 py-2 text-right align-top augur-tabular font-semibold">{fmtProb(row.p_market)}</td>
              <td className="px-3 py-2 text-right align-top">
                {row.augur_context ? (
                  <>
                    <div className="augur-tabular font-semibold augur-accent-text">
                      {row.augur_context.p_model == null ? "n/a" : fmtProb(row.augur_context.p_model)}
                    </div>
                    <div className="mt-0.5 text-[11px] augur-muted" title={row.augur_context.note}>
                      {row.augur_context.signal}
                    </div>
                  </>
                ) : (
                  <span className="augur-muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
