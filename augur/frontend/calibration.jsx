import React, { useEffect, useMemo, useState } from "react";

import { fetchCalibrationRun } from "./client.js";
import { NumberField } from "./lib/controls.jsx";
import { fmtPct } from "./lib/format.js";
import { MetricFanChart } from "./fan_chart.jsx";
import { RolloutResultsSkeleton } from "./skeleton.jsx";
import { CurrencyDisplayProvider } from "./hooks.js";
import { FAN_PERCENTILES, clampRolloutCount, clampHorizonMonths } from "./input_helpers.js";
import { markFanRows } from "./data_helpers.js";

// The issuer mark fan is a per-unit USD price. `chartValue` ending in `Usd` makes the shared
// `MetricFanChart` axis/tooltip format it as currency; the label keeps it honest as a per-unit
// mark (NOT a valuation — augur models no shares / market cap).
const MARK_METRIC = { value: "mark_usd_per_unit", chartValue: "markUsd", label: "Per-unit mark" };

// `rollouts` and `horizonMonths` are intentionally absent: both are tab-shared controls owned by
// the app shell (see `rolloutCountFromSearch` / `horizonMonthsFromSearch`), passed in as props and
// woven into the run request below. Only the seed (calibration-specific, tucked away) lives here.
const CALIBRATION_INPUT_DEFAULTS = {
  seed: 1701,
};

function fmtProb(value) {
  return value == null || !Number.isFinite(Number(value)) ? "n/a" : fmtPct(value);
}

// `D_KL(market ‖ model)` in bits: 0 = model matches the market, larger = louder disagreement.
function fmtBits(value) {
  return value == null || !Number.isFinite(Number(value)) ? "—" : `${Number(value).toFixed(3)} bits`;
}

// Bigger model-vs-market divergences get a louder tint so the eye lands on the disagreements
// first. Thresholds are in bits of D_KL (≈0.03 bits ≈ a 0.4-vs-0.25 forecast gap).
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

function CalibrationForm({ input, catalog, exogenousModel, onChange }) {
  return (
    <aside className="min-w-0">
      <div className="augur-card divide-y divide-slate-200 dark:divide-slate-700">
        <div className="px-4 py-3">
          <div className="augur-eyebrow">Calibration run</div>
          <div className="mt-1 text-xs augur-muted">
            Score a built-in exogenous model's rollouts against this deployment's curated prediction-market catalog
            (exogenous-only — no portfolio, no product scenario). Results update live as you tune the inputs.
          </div>
        </div>
        <div className="grid gap-3 px-4 py-3">
          <div data-calibration-catalog={catalog.issuer}>
            <div className="augur-eyebrow">Market catalog</div>
            <div className="mt-1 text-sm font-semibold augur-strong">{catalog.label}</div>
            <div className="text-xs augur-muted">issuer: {catalog.issuer}</div>
          </div>
          <div>
            <div className="augur-eyebrow">Exogenous model</div>
            <div className="mt-1 text-sm font-semibold augur-strong" data-calibration-model={exogenousModel ?? ""}>
              {exogenousModel ?? "(no presets)"}
            </div>
          </div>
          <div className="text-xs augur-muted">
            Horizon and rollouts are set in the header (shared with the product tab).
          </div>
        </div>
        <details className="px-4 py-3 [&_summary::-webkit-details-marker]:hidden">
          <summary className="augur-eyebrow cursor-pointer list-none">
            <span className="inline-flex items-center gap-1">
              <span aria-hidden="true" className="transition-transform [details[open]_&]:rotate-90">
                ▸
              </span>
              Advanced
            </span>
          </summary>
          <div className="mt-3">
            <NumberField
              label="Seed"
              value={input.seed}
              min={0}
              max={2 ** 31 - 1}
              step={1}
              onChange={(seed) => onChange({ seed })}
            />
          </div>
        </details>
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
              <tr key={row.slug} className={klToneClass(row.klBits)} data-calibration-clean-row={row.slug}>
                <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                  <a href={row.url} target="_blank" rel="noreferrer" className="augur-accent-text hover:underline">
                    {row.question}
                  </a>
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
            <tr key={row.slug} data-calibration-surfaced-row={row.slug}>
              <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                <a href={row.url} target="_blank" rel="noreferrer" className="augur-accent-text hover:underline">
                  {row.question}
                </a>
                <div className="mt-0.5 text-[11px] font-normal augur-muted">
                  <span className="font-semibold">{row.mappability}</span>
                  {row.correlateOf ? ` · correlate of ${row.correlateOf}` : ""}
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

function MarkFanPanel({ markFan, metricScale }) {
  const rows = useMemo(() => markFanRows(markFan), [markFan]);
  const percentiles = markFan?.percentiles?.length ? markFan.percentiles : FAN_PERCENTILES;
  return (
    <section className="augur-panel overflow-hidden" aria-label="Issuer mark fan" data-calibration-mark-fan="">
      <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="augur-eyebrow">Issuer per-unit mark</div>
        <div className="mt-1 text-xs augur-muted">
          Percentile bands of {markFan.issuer}'s modelled per-unit mark over the horizon. This is a per-UNIT price, NOT
          a company valuation (augur models no shares or market cap).
        </div>
      </div>
      {rows.length > 0 ? (
        <MetricFanChart
          rows={rows}
          metric={MARK_METRIC}
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
        <div className="flex min-h-[18rem] items-center justify-center text-sm augur-muted">No mark fan data.</div>
      )}
    </section>
  );
}

function CalibrationResults({ response, metricScale }) {
  const { result, markFan } = response;
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
          <div className="mt-2 text-sm font-semibold augur-tabular">Manifold live prices</div>
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

      <MarkFanPanel markFan={markFan} metricScale={metricScale} />

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

export function CalibrationWorkspace({ bootstrap, rolloutCount, exogenousModel, horizonMonths, metricScale }) {
  const catalog = bootstrap.calibration ?? null;

  const [input, setInput] = useState({ ...CALIBRATION_INPUT_DEFAULTS });
  const [response, setResponse] = useState(null);
  const [runError, setRunError] = useState(null);

  const updateInput = (patch) => setInput((previous) => ({ ...previous, ...patch }));

  // The calibration run is fully determined by the seed plus the tab-shared controls — the exogenous
  // model (`?x=`), rollout count (`?n=`), and horizon (`?h=`), all owned by the app shell. Memoizing
  // keeps the auto-run effect from re-firing on unrelated re-renders (it keys on this request).
  const rollouts = clampRolloutCount(rolloutCount, bootstrap);
  const horizon = clampHorizonMonths(horizonMonths, bootstrap);
  const request = useMemo(
    () => ({
      presetId: exogenousModel,
      horizonMonths: horizon,
      rollouts,
      seed: input.seed,
    }),
    [exogenousModel, horizon, rollouts, input.seed]
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

  const currencyDisplayContext = useMemo(() => ({ display: "compact", setDisplay: () => {} }), []);

  if (!catalog) {
    return (
      <div className="augur-note p-4" data-calibration-unconfigured="">
        This deployment has no calibration catalog configured.
      </div>
    );
  }

  return (
    <CurrencyDisplayProvider value={currencyDisplayContext}>
      <div className="min-w-0 space-y-5">
        <section className="grid min-w-0 gap-5 min-[864px]:grid-cols-[28rem_minmax(0,1fr)]">
          <CalibrationForm input={input} catalog={catalog} exogenousModel={exogenousModel} onChange={updateInput} />

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
    </CurrencyDisplayProvider>
  );
}
