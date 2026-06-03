import React, { useEffect, useMemo, useState } from "react";

import { fetchCalibrationRun } from "./client.ts";
import { fmtPct, fmtVolume } from "./lib/format.ts";
import { toastFetchError } from "./lib/toast.ts";
import { MetricFanChart } from "./fan_chart.tsx";
import { RolloutResultsSkeleton } from "./skeleton.tsx";
import { FAN_PERCENTILES, clampRolloutCount, clampFirstSeed, clampHorizonMonths } from "./input_helpers.ts";
import { markFanRows } from "./data_helpers.ts";
import { sortSanityBands, sanityPassCount, fmtExpectedBand, fmtObserved } from "./sanity_bands.ts";

// `chartValue` ending in `Usd` makes the shared `MetricFanChart` axis/tooltip format these
// issuer channels as currency.
const MARK_METRIC = { value: "mark_usd_per_unit", chartValue: "markUsd", label: "Per-unit mark" };
const VALUATION_METRIC = {
  value: "company_valuation_usd",
  chartValue: "companyValuationUsd",
  label: "Company valuation",
};

function fmtProb(value) {
  return value == null || !Number.isFinite(Number(value)) ? "n/a" : fmtPct(value);
}

// `D_KL(market ‖ model)` in bits: 0 = model matches the market, larger = louder disagreement.
function fmtBits(value) {
  return value == null || !Number.isFinite(Number(value)) ? "—" : `${Number(value).toFixed(3)} bits`;
}

// Bigger model-vs-market divergences get a louder tint on the KL cell itself (see
// `klTextClass`) so the eye lands on the disagreements first. The full row stays untinted so
// platform-logo and link contrast doesn't have to fight a rose/amber background.
function klTextClass(klBits) {
  if (klBits == null || !Number.isFinite(Number(klBits))) return "augur-muted";
  if (klBits >= 0.15) return "font-semibold text-rose-700 dark:text-rose-300";
  if (klBits >= 0.05) return "font-semibold text-amber-700 dark:text-amber-300";
  return "augur-tabular";
}

// Each platform's actual brand mark, trimmed to the icon glyph only:
//   - manifold: the official "crane" logo from manifold.markets/logo.svg (indigo #4337C9 on
//     light) and logo-white.svg (white on dark); same path either way, only stroke color flips.
//   - polymarket: the trapezoidal icon extracted from the Wikimedia logo SVG
//     (dropping the "polymarket" wordmark that sits beside it).
//   - kalshi: the lowercase "k" subpath extracted from the Wikimedia wordmark
//     (Kalshi only ships a wordmark, so this is the most icon-like fragment).
// Each viewBox is square so the three icons rendered at h-5 w-5 occupy the same physical
// footprint; `stroke|fill="currentColor"` (where used) lets the wrapper drive the color so
// dark-mode swaps stay declarative.
const PLATFORM_ICON = {
  manifold: (
    <svg
      viewBox="0 0 24 24"
      className="h-5 w-5 shrink-0 text-[#4337C9] dark:text-white"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M5.24854 17.0952L18.7175 6.80301L14.3444 20M5.24854 17.0952L9.79649 18.5476M5.24854 17.0952L4.27398 6.52755M14.3444 20L9.79649 18.5476M14.3444 20L22 12.638L16.3935 13.8147M9.79649 18.5476L12.3953 15.0668M4.27398 6.52755L10.0714 13.389M4.27398 6.52755L2 9.0818L4.47389 8.85643M12.9451 11.1603L10.971 5L8.65369 11.6611" />
    </svg>
  ),
  polymarket: (
    // Pad the 137x168 icon out to a 168x168 square so it centers in the same w-4 box as the
    // other two; shift x by -15.5 (= (168-137)/2) so the trapezoid sits in the middle.
    <svg
      viewBox="-15.5 0 168 168"
      className="h-5 w-5 shrink-0 text-slate-800 dark:text-slate-200"
      fill="currentColor"
      aria-hidden
    >
      <path d="M136.267 152.495C136.267 159.76 136.267 163.392 133.891 165.192C131.516 166.993 128.019 166.012 121.024 164.049L8.63192 132.51C4.41793 131.328 2.31093 130.737 1.09248 129.129C-0.125977 127.522 -0.125977 125.333 -0.125977 120.957V47.0434C-0.125977 42.6667 -0.125977 40.4783 1.09248 38.8709C2.31093 37.2634 4.41792 36.6722 8.63191 35.4897L121.024 3.95096C128.019 1.98834 131.516 1.00703 133.891 2.80771C136.267 4.60839 136.267 8.24049 136.267 15.5047V152.495ZM27.9043 122.228L120.966 148.345V96.1133L27.9043 122.228ZM15.1738 110.111L108.217 84L15.1738 57.8887V110.111ZM27.9033 45.7725L120.966 71.8877V19.6553L27.9033 45.7725Z" />
    </svg>
  ),
  kalshi: (
    // Pad the 180x226 "k" out to a 226x226 square so it centers in the same w-4 box; shift x
    // by -23 (= (226-180)/2). Brand green #00DD94 reads on both light and dark backgrounds.
    <svg viewBox="-23 0 226 226" className="h-5 w-5 shrink-0" fill="#00DD94" aria-hidden>
      <path d="M105.23 105.628L179.66 222.61H115.118L54.3009 121.934V222.61H0V3.38607H54.3009V99.102L119.464 3.38607H177.489L105.23 105.628Z" />
    </svg>
  ),
};

function PlatformBadge({ platform }) {
  const icon = PLATFORM_ICON[platform] ?? PLATFORM_ICON.manifold;
  // Centered inside the dedicated platform column; the parent <td> handles spacing.
  return (
    <span className="inline-flex items-center justify-center" title={platform}>
      {icon}
    </span>
  );
}

// Reasonableness-band status → pill classes. A failing band reads loudest (rose), a passing
// band reassuring (emerald), a skipped band muted (slate), an unmodeled band a distinct amber
// (the spec asked for a series the preset can't emit — a config-shape signal, not a model
// reading) — the same rose/amber/muted family the KL tints above draw from.
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

function CalibrationForm({ catalog }) {
  return (
    <aside className="min-w-0">
      <div className="augur-card">
        <div className="grid gap-3 px-4 py-3">
          <div data-calibration-catalog={(catalog.issuers ?? []).join(",")}>
            <div className="augur-eyebrow">Market catalog</div>
            <div className="mt-1 text-sm font-semibold augur-strong">{catalog.label}</div>
            {(catalog.issuers ?? []).length > 0 && (
              <div className="text-xs augur-muted">issuers: {(catalog.issuers ?? []).join(", ")}</div>
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}

function CleanTable({ rows }) {
  if (rows.length === 0) {
    return <div className="px-4 py-6 text-sm augur-muted">No apples-to-apples (scored) markets in this catalog.</div>;
  }
  // Loudest disagreements first; rows the model couldn't resolve (no `klBits`) sink to the bottom.
  const sorted = rows.slice().sort((left, right) => (right.klBits ?? -Infinity) - (left.klBits ?? -Infinity));
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
              KL
            </th>
            <th className="px-3 py-2 text-right font-semibold">Unresolved</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {sorted.map((row) => {
            const ci = row.ci95 ?? [];
            const unresolvedPct =
              row.nResolved + row.unresolved > 0 ? row.unresolved / (row.nResolved + row.unresolved) : null;
            return (
              <tr key={row.marketId} data-calibration-clean-row={row.marketId}>
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
                  {fmtVolume(row.volume, row.volumeUnit) && (
                    <div
                      className="mt-0.5 text-[11px] font-normal augur-muted augur-tabular"
                      title="total all-time volume traded on the platform"
                    >
                      {fmtVolume(row.volume, row.volumeUnit)}
                    </div>
                  )}
                </th>
                <td className="px-3 py-2 text-right augur-tabular">{fmtProb(row.pMarket)}</td>
                <td className="px-3 py-2 text-right augur-tabular">
                  {row.pModel == null ? (
                    <span className="augur-muted">n/a</span>
                  ) : (
                    <>
                      {fmtProb(row.pModel)}
                      {ci.length === 2 && (
                        <span className="ml-1 text-xs augur-muted">
                          [{fmtProb(ci[0])}–{fmtProb(ci[1])}]
                        </span>
                      )}
                    </>
                  )}
                </td>
                <td className={`px-3 py-2 text-right ${klTextClass(row.klBits)}`}>{fmtBits(row.klBits)}</td>
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

function SurfacedTable({ rows }) {
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
            <tr key={row.marketId} data-calibration-surfaced-row={row.marketId}>
              <td className="px-2 py-2 text-center align-top">
                <PlatformBadge platform={row.platform} />
              </td>
              <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                <a href={row.url} target="_blank" rel="noreferrer" className="augur-accent-text hover:underline">
                  {row.question}
                </a>
                <div className="mt-0.5 text-[11px] font-normal augur-muted">
                  <span className="font-semibold">
                    {row.correlateOf ? `correlate of ${row.correlateOf}` : row.mappability}
                  </span>
                  {fmtVolume(row.volume, row.volumeUnit) && (
                    <span className="ml-2 augur-tabular" title="total all-time volume traded on the platform">
                      · {fmtVolume(row.volume, row.volumeUnit)}
                    </span>
                  )}
                </div>
                {row.reason && <div className="mt-0.5 text-xs font-normal augur-body">{row.reason}</div>}
              </th>
              <td className="px-3 py-2 text-right align-top augur-tabular font-semibold">{fmtProb(row.pMarket)}</td>
              <td className="px-3 py-2 text-right align-top">
                {row.augurContext ? (
                  <>
                    <div className="augur-tabular font-semibold augur-accent-text">
                      {row.augurContext.pModel == null ? "n/a" : fmtProb(row.augurContext.pModel)}
                    </div>
                    <div className="mt-0.5 text-[11px] augur-muted" title={row.augurContext.note}>
                      {row.augurContext.signal}
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

function IssuerFanPanel({ fan, metric, title, description, metricScale, dataAttribute, emptyLabel }) {
  const rows = useMemo(() => markFanRows(fan), [fan]);
  const percentiles = fan?.percentiles?.length ? fan.percentiles : FAN_PERCENTILES;
  return (
    <section className="augur-panel overflow-hidden" aria-label={title} {...{ [dataAttribute]: "" }}>
      <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="augur-eyebrow">{title}</div>
        <div className="mt-1 text-xs augur-muted">{description}</div>
      </div>
      {rows.length > 0 ? (
        <MetricFanChart
          series={[{ id: "mark", label: title, color: "#1d4ed8", rows, isActive: true }]}
          metric={metric}
          metricScale={metricScale}
          percentiles={percentiles}
          selectedRows={[]}
          selectedEvents={[]}
          selectedSeed={null}
          selectedFailed={false}
          visibleEventKinds={new Set()}
          selectedEventMonthIndex={null}
          hoveredEventMonthIndex={null}
          onSelectEventMonth={() => {}}
          onHoverEventMonth={() => {}}
        />
      ) : (
        <div className="flex min-h-[18rem] items-center justify-center text-sm augur-muted">{emptyLabel}</div>
      )}
    </section>
  );
}

// The deployment's hardcoded `sample_sanity` reasonableness bands evaluated against THIS run's
// rollouts (same expected-range-vs-observed shape as the model-vs-market table above). Renders
// nothing when the deployment configured no bands (`sanityBands` empty/absent).
function SanityBandsPanel({ bands }) {
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

// One mutually-exclusive bucket range, e.g. `[7400, 7600)` or open ends `< 4000` / `≥ 9000`.
function fmtRange(low, high) {
  if (low == null) return `< ${Number(high).toLocaleString()}`;
  if (high == null) return `≥ ${Number(low).toLocaleString()}`;
  return `${Number(low).toLocaleString()}–${Number(high).toLocaleString()}`;
}

// A Kalshi/Polymarket range family scored as one multinomial D_KL(market ‖ model): the
// normalized per-bucket market shares vs the model's per-bucket rollout shares at a date.
function CategoricalPanel({ families }) {
  return (
    <section className="augur-panel overflow-hidden" aria-label="Categorical markets" data-calibration-categorical="">
      <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="augur-eyebrow">Categorical markets (multinomial)</div>
        <div className="mt-1 text-xs augur-muted">
          Mutually-exclusive range families scored as one multinomial KL = D<sub>KL</sub>(market ‖ model) over the
          buckets at a single date.
        </div>
      </div>
      <div className="divide-y divide-slate-200 dark:divide-slate-700">
        {families.map((family) => (
          <div key={family.familyId} data-calibration-categorical-family={family.familyId} className="px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <PlatformBadge platform={family.platform} />
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold augur-strong">{family.question}</div>
                  <div className="text-[11px] augur-muted">
                    {family.channel} · {family.atDate}
                  </div>
                </div>
              </div>
              <div className={`shrink-0 text-right text-sm ${klTextClass(family.klBits)}`}>{fmtBits(family.klBits)}</div>
            </div>
            <table className="mt-2 min-w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  <th className="py-1 font-semibold">Bucket</th>
                  <th className="py-1 text-right font-semibold">Market</th>
                  <th className="py-1 text-right font-semibold">Model</th>
                </tr>
              </thead>
              <tbody>
                {family.buckets.map((bucket) => (
                  <tr key={bucket.marketId} data-calibration-bucket={bucket.marketId}>
                    <td className="py-1 augur-tabular">{fmtRange(bucket.low, bucket.high)}</td>
                    <td className="py-1 text-right augur-tabular">{fmtProb(bucket.pMarket)}</td>
                    <td className="py-1 text-right augur-tabular">
                      {bucket.pModel == null ? <span className="augur-muted">n/a</span> : fmtProb(bucket.pModel)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </section>
  );
}

function CalibrationResults({ response, metricScale }) {
  const { result, markFans, valuationFans } = response;
  return (
    <div className="min-w-0 space-y-5">
      <div className="augur-card p-4">
        <div className="augur-eyebrow">Model calibration</div>
        <div className="mt-1 text-xs augur-muted">as of {result.asOf}</div>
      </div>

      <section className="augur-panel overflow-hidden" aria-label="Scored markets">
        <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
          <div className="augur-eyebrow">Scored markets (model vs market)</div>
          <div className="mt-1 text-xs augur-muted">
            Apples-to-apples: markets augur models as events. KL = D<sub>KL</sub>(market ‖ model) in bits, the
            model-vs-market disagreement we optimize. Sorted loudest-first.
          </div>
        </div>
        <CleanTable rows={result.clean ?? []} />
      </section>

      {result.categorical?.length > 0 && <CategoricalPanel families={result.categorical} />}

      {(markFans ?? []).map((fan) => (
        <IssuerFanPanel
          key={`mark-${fan.issuer}`}
          fan={fan}
          metric={MARK_METRIC}
          title={`Per-unit mark — ${fan.issuer}`}
          description={`Percentile bands of ${fan.issuer}'s modelled per-unit mark over the horizon.`}
          metricScale={metricScale}
          dataAttribute="data-calibration-mark-fan"
          emptyLabel="No mark fan data."
        />
      ))}

      {(valuationFans ?? []).map((fan) => (
        <IssuerFanPanel
          key={`val-${fan.issuer}`}
          fan={fan}
          metric={VALUATION_METRIC}
          title={`Company valuation — ${fan.issuer}`}
          description={`Percentile bands of ${fan.issuer}'s modelled company valuation over the horizon.`}
          metricScale={metricScale}
          dataAttribute="data-calibration-valuation-fan"
          emptyLabel="No valuation fan data."
        />
      ))}

      {response.sanityBands?.length > 0 && <SanityBandsPanel bands={response.sanityBands} />}

      <section className="augur-panel overflow-hidden" aria-label="Surfaced markets">
        <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
          <div className="augur-eyebrow">Surfaced markets (not scored / context only)</div>
          <div className="mt-1 text-xs augur-muted">
            Markets augur has no event concept for. The market price sits beside a related (NOT equal) augur signal
            where one exists.
          </div>
        </div>
        <SurfacedTable rows={result.surfaced ?? []} />
      </section>
    </div>
  );
}

export function CalibrationWorkspace({
  bootstrap,
  rolloutCount,
  firstSeed,
  model,
  horizonMonths,
  metricScale,
  sharedControlsSlot,
}) {
  const catalog = bootstrap.calibration ?? null;

  const [response, setResponse] = useState(null);
  const [runError, setRunError] = useState(null);

  // The calibration run is fully determined by tab-shared shell controls: exogenous model (`?x=`),
  // rollout count (`?n=`), and horizon (`?h=`), plus the fixed first seed. Memoizing keeps the
  // auto-run effect from re-firing on unrelated re-renders (it keys on this request).
  const rollouts = clampRolloutCount(rolloutCount, bootstrap);
  const seed = clampFirstSeed(firstSeed);
  const horizon = clampHorizonMonths(horizonMonths, bootstrap);
  const request = useMemo(
    () => ({
      presetId: model,
      horizonMonths: horizon,
      rollouts,
      seed,
    }),
    [model, horizon, rollouts, seed]
  );

  // Live auto-run (no button): debounce input changes, abort the in-flight run, and re-score
  // on every settled request — mirrors the product page's metric-fan auto-refresh.
  useEffect(() => {
    if (!catalog || !request.presetId) return undefined;
    const controller = new AbortController();
    setResponse(null);
    const handle = setTimeout(() => {
      fetchCalibrationRun(request, { signal: controller.signal })
        .then((payload) => {
          setResponse(payload);
          setRunError(null);
        })
        .catch((error) => {
          if (error?.name === "AbortError") return;
          setResponse(null);
          setRunError(error?.message || String(error));
          toastFetchError("calibration-run", "Calibration run failed", error);
        });
    }, 120);
    return () => {
      clearTimeout(handle);
      controller.abort();
    };
  }, [catalog, request]);

  if (!catalog) {
    return (
      <div className="augur-note p-4" data-calibration-unconfigured="">
        This deployment has no calibration catalog configured.
      </div>
    );
  }

  return (
    <div className="min-w-0 space-y-5">
      <section className="grid min-w-0 gap-5 min-[864px]:grid-cols-[28rem_minmax(0,1fr)]">
        <div className="min-w-0 space-y-5">
          {sharedControlsSlot}
          <CalibrationForm catalog={catalog} />
        </div>

        <div className="min-w-0 space-y-5">
          {runError ? (
            <div className="augur-note-danger p-4 text-sm">Calibration run failed: {runError}</div>
          ) : response ? (
            <CalibrationResults response={response} metricScale={metricScale} />
          ) : (
            <RolloutResultsSkeleton />
          )}
        </div>
      </section>
    </div>
  );
}
