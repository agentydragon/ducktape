import React, { useEffect, useMemo, useState } from "react";

import { fetchCalibrationRun } from "./client.ts";
import { fmtPct } from "./lib/format.ts";
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

// Bigger model-vs-market divergences get a louder tint so the eye lands on the disagreements
// first. Thresholds are in bits of D_KL: amber ≥0.05 bits (≈ a market at 0.50 vs model 0.37),
// rose ≥0.15 bits (≈ 0.50 vs 0.29).
function klToneClass(klBits) {
  if (klBits == null || !Number.isFinite(Number(klBits))) return "";
  if (klBits >= 0.15) return "bg-rose-50 dark:bg-rose-950/30";
  if (klBits >= 0.05) return "bg-amber-50 dark:bg-amber-950/30";
  return "";
}

function klTextClass(klBits) {
  if (klBits == null || !Number.isFinite(Number(klBits))) return "augur-muted";
  if (klBits >= 0.15) return "font-semibold text-rose-700 dark:text-rose-300";
  if (klBits >= 0.05) return "font-semibold text-amber-700 dark:text-amber-300";
  return "augur-tabular";
}

const PLATFORM_STYLE = {
  manifold: "bg-blue-50 text-blue-700 ring-blue-600/20 dark:bg-blue-950/40 dark:text-blue-300 dark:ring-blue-400/20",
  polymarket:
    "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-400/20",
  kalshi:
    "bg-purple-50 text-purple-700 ring-purple-600/20 dark:bg-purple-950/40 dark:text-purple-300 dark:ring-purple-400/20",
};

// Minimal inline SVG icons — each platform's logo reduced to a recognizable glyph.
const PLATFORM_ICON = {
  manifold: (
    <svg viewBox="0 0 16 16" className="h-3 w-3 shrink-0" fill="currentColor">
      <path d="M8 1L14.9 12.5H1.1L8 1Z" />
    </svg>
  ),
  polymarket: (
    <svg viewBox="0 0 16 16" className="h-3 w-3 shrink-0" fill="currentColor">
      <path d="M3 3h4v4H3V3Zm6 0h4v4H9V3ZM3 9h4v4H3V9Zm6 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" />
    </svg>
  ),
  kalshi: (
    <svg viewBox="0 0 16 16" className="h-3 w-3 shrink-0" fill="currentColor">
      <path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1Zm0 2a5 5 0 0 1 3.54 8.54L5.46 5.46A5 5 0 0 1 8 3Z" />
    </svg>
  ),
};

function PlatformBadge({ platform }) {
  const tone = PLATFORM_STYLE[platform] ?? PLATFORM_STYLE.manifold;
  const icon = PLATFORM_ICON[platform];
  return (
    <span
      className={`ml-1 inline-flex items-center gap-0.5 rounded-full px-1.5 py-px text-[10px] font-semibold ring-1 ring-inset ${tone}`}
    >
      {icon}
      {platform}
    </span>
  );
}

// Reasonableness-band status → pill classes. A failing band reads loudest (rose), a passing
// band reassuring (emerald), a skipped band muted (slate) — the same rose/amber/muted family
// the KL tints above draw from.
const SANITY_PILL_CLASS = {
  pass: "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-400/20",
  fail: "bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-950/40 dark:text-rose-300 dark:ring-rose-400/20",
  skipped: "bg-slate-100 text-slate-600 ring-slate-500/20 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-400/20",
};

// A failing band also tints its whole row, mirroring `klToneClass` for loud KL.
function sanityRowToneClass(status) {
  return status === "fail" ? "bg-rose-50 dark:bg-rose-950/30" : "";
}

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
        <div className="px-4 py-3">
          <div className="augur-eyebrow">Calibration run</div>
          <div className="mt-1 text-xs augur-muted">
            Score a built-in exogenous model&apos;s rollouts against this deployment&apos;s curated prediction-market
            catalog (exogenous-only — no portfolio, no product scenario). Results update live as you tune the inputs.
          </div>
        </div>
        <div className="grid gap-3 px-4 py-3">
          <div data-calibration-catalog={catalog.issuer}>
            <div className="augur-eyebrow">Market catalog</div>
            <div className="mt-1 text-sm font-semibold augur-strong">{catalog.label}</div>
            <div className="text-xs augur-muted">issuer: {catalog.issuer}</div>
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
              <tr key={row.marketId} className={klToneClass(row.klBits)} data-calibration-clean-row={row.marketId}>
                <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                  <a href={row.url} target="_blank" rel="noreferrer" className="augur-accent-text hover:underline">
                    {row.question}
                  </a>
                  <PlatformBadge platform={row.platform} />
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
            <th className="px-4 py-2 font-semibold">Market</th>
            <th className="px-3 py-2 text-right font-semibold">Market</th>
            <th className="px-3 py-2 text-right font-semibold">Augur signal</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {rows.map((row) => (
            <tr key={row.marketId} data-calibration-surfaced-row={row.marketId}>
              <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                <a href={row.url} target="_blank" rel="noreferrer" className="augur-accent-text hover:underline">
                  {row.question}
                </a>
                <PlatformBadge platform={row.platform} />
                <div className="mt-0.5 text-[11px] font-normal augur-muted">
                  <span className="font-semibold">
                    {row.correlateOf ? `correlate of ${row.correlateOf}` : row.mappability}
                  </span>
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
                className={sanityRowToneClass(band.status)}
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

function CalibrationResults({ response, metricScale }) {
  const { result, markFan, valuationFan } = response;
  return (
    <div className="min-w-0 space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="augur-card p-4">
          <div className="augur-eyebrow">Issuer</div>
          <div className="mt-2 text-2xl font-semibold augur-tabular">{result.issuer}</div>
          <div className="mt-1 text-xs augur-muted">as of {result.asOf}</div>
        </div>
        <div className="augur-card p-4">
          <div className="augur-eyebrow">Price source</div>
          <div className="mt-2 text-sm font-semibold augur-tabular">Live prediction-market prices</div>
        </div>
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

      <IssuerFanPanel
        fan={markFan}
        metric={MARK_METRIC}
        title="Issuer per-unit mark"
        description={`Percentile bands of ${markFan.issuer}'s modelled per-unit mark over the horizon.`}
        metricScale={metricScale}
        dataAttribute="data-calibration-mark-fan"
        emptyLabel="No mark fan data."
      />

      {valuationFan && (
        <IssuerFanPanel
          fan={valuationFan}
          metric={VALUATION_METRIC}
          title="Issuer company valuation"
          description={`Percentile bands of ${valuationFan.issuer}'s modelled company valuation over the horizon.`}
          metricScale={metricScale}
          dataAttribute="data-calibration-valuation-fan"
          emptyLabel="No valuation fan data."
        />
      )}

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
  // rollout count (`?n=`), first seed (`?seed=`), and horizon (`?h=`). Memoizing keeps the auto-run
  // effect from re-firing on unrelated re-renders (it keys on this request).
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
