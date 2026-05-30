import React, { useEffect, useMemo, useState } from "react";

import { fetchCalibrationRun } from "./client.js";
import { NativeSelectField, NumberField } from "./lib/controls.jsx";
import { fmtPct } from "./lib/format.js";
import { MetricFanChart } from "./fan_chart.jsx";
import { RolloutResultsSkeleton } from "./skeleton.jsx";
import { CurrencyDisplayProvider } from "./hooks.js";
import { FAN_PERCENTILES } from "./input_helpers.js";
import { markFanRows } from "./data_helpers.js";

// The issuer mark fan is a per-unit USD price. `chartValue` ending in `Usd` makes the shared
// `MetricFanChart` axis/tooltip format it as currency; the label keeps it honest as a per-unit
// mark (NOT a valuation — augur models no shares / market cap).
const MARK_METRIC = { value: "mark_usd_per_unit", chartValue: "markUsd", label: "Per-unit mark" };

const CALIBRATION_INPUT_DEFAULTS = {
  horizonMonths: 120,
  rollouts: 2000,
  seed: 1701,
};

function fmtProb(value) {
  return value == null || !Number.isFinite(Number(value)) ? "n/a" : fmtPct(value);
}

function fmtDeadline(value) {
  return value ? String(value) : "—";
}

// Bigger model-vs-market gaps get a louder tint so the eye lands on the disagreements first.
function gapToneClass(absGap) {
  if (absGap == null || !Number.isFinite(Number(absGap))) return "";
  if (absGap >= 0.3) return "bg-rose-50 dark:bg-rose-950/30";
  if (absGap >= 0.15) return "bg-amber-50 dark:bg-amber-950/30";
  return "";
}

function gapTextClass(absGap) {
  if (absGap == null || !Number.isFinite(Number(absGap))) return "augur-muted";
  if (absGap >= 0.3) return "font-semibold text-rose-700 dark:text-rose-300";
  if (absGap >= 0.15) return "font-semibold text-amber-700 dark:text-amber-300";
  return "augur-tabular";
}

function CalibrationForm({ input, catalog, presets, defaultPresetId, onChange }) {
  const presetOptions = presets.map((preset) => ({ value: preset, label: preset }));
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
          <NativeSelectField
            label="Model preset"
            aria-label="Model preset"
            description={defaultPresetId ? `Deployment default: ${defaultPresetId}` : undefined}
            value={input.presetId ?? ""}
            disabled={presets.length === 0}
            data={presets.length === 0 ? [{ value: "", label: "(no presets)" }] : presetOptions}
            onChange={(event) => onChange({ presetId: event.target.value || null })}
          />
        </div>
        <div className="grid gap-3 px-4 py-3 sm:grid-cols-2 min-[864px]:grid-cols-1 2xl:grid-cols-2">
          <NumberField
            label="Horizon"
            value={input.horizonMonths}
            min={1}
            max={1200}
            step={12}
            suffix="mo"
            onChange={(horizonMonths) => onChange({ horizonMonths })}
          />
          <NumberField
            label="Rollouts"
            value={input.rollouts}
            min={1}
            step={500}
            onChange={(rollouts) => onChange({ rollouts })}
          />
          <NumberField
            label="Seed"
            value={input.seed}
            min={0}
            max={2 ** 31 - 1}
            step={1}
            onChange={(seed) => onChange({ seed })}
          />
        </div>
      </div>
    </aside>
  );
}

function CleanTable({ rows }) {
  if (rows.length === 0) {
    return <div className="px-4 py-6 text-sm augur-muted">No apples-to-apples (scored) markets in this catalog.</div>;
  }
  // Loudest disagreements first; rows the model couldn't resolve (no `absGap`) sink to the bottom.
  const sorted = rows.slice().sort((left, right) => (right.absGap ?? -Infinity) - (left.absGap ?? -Infinity));
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full border-t border-slate-200 text-sm dark:border-slate-700">
        <thead>
          <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            <th className="px-4 py-2 font-semibold">Market</th>
            <th className="px-3 py-2 text-left font-semibold">Mapping</th>
            <th className="px-3 py-2 text-right font-semibold">Deadline</th>
            <th className="px-3 py-2 text-right font-semibold">Market</th>
            <th className="px-3 py-2 text-right font-semibold">Model (95% CI)</th>
            <th className="px-3 py-2 text-right font-semibold">|gap|</th>
            <th className="px-3 py-2 text-right font-semibold">Unresolved</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {sorted.map((row) => {
            const ci = row.ci95 ?? [];
            const unresolvedPct =
              row.nResolved + row.unresolved > 0 ? row.unresolved / (row.nResolved + row.unresolved) : null;
            return (
              <tr key={row.slug} className={gapToneClass(row.absGap)} data-calibration-clean-row={row.slug}>
                <th className="px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                  <a href={row.url} target="_blank" rel="noreferrer" className="augur-accent-text hover:underline">
                    {row.question}
                  </a>
                </th>
                <td className="px-3 py-2 text-left augur-muted">{row.mappingKind}</td>
                <td className="px-3 py-2 text-right augur-tabular augur-muted">
                  {fmtDeadline(row.resolutionDeadline)}
                </td>
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
                <td className={`px-3 py-2 text-right ${gapTextClass(row.absGap)}`}>
                  {row.absGap == null ? "—" : fmtProb(row.absGap)}
                </td>
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

function SurfacedList({ rows }) {
  if (rows.length === 0) {
    return <div className="px-4 py-6 text-sm augur-muted">No surfaced (context-only) markets in this catalog.</div>;
  }
  return (
    <ul className="divide-y divide-slate-100 dark:divide-slate-800">
      {rows.map((row) => (
        <li key={row.slug} className="px-4 py-3" data-calibration-surfaced-row={row.slug}>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div className="min-w-0">
              <div className="font-semibold augur-strong">{row.question}</div>
              <div className="text-xs augur-muted">
                {row.slug}
                {" · "}
                <span className="font-semibold">{row.mappability}</span>
                {row.correlateOf ? ` · correlate of ${row.correlateOf}` : ""}
              </div>
            </div>
            <div className="whitespace-nowrap text-right">
              <div className="augur-tabular font-semibold">{fmtProb(row.pMarket)}</div>
              <div className="text-[11px] uppercase tracking-wide augur-muted">market</div>
            </div>
          </div>
          {row.reason && <div className="mt-1 text-xs augur-body">{row.reason}</div>}
          {row.augurContext && (
            <div className="mt-2 rounded border border-blue-200 bg-blue-50 px-3 py-2 text-xs dark:border-sky-400/20 dark:bg-sky-950/20">
              <div className="font-semibold augur-accent-text">
                Augur signal (related, not scored): {row.augurContext.signal}
              </div>
              <div className="mt-0.5 augur-body">
                {row.augurContext.pModel == null ? "n/a" : fmtProb(row.augurContext.pModel)} · {row.augurContext.note}
              </div>
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function MarkFanPanel({ markFan }) {
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
          metricScale="linear"
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

function CalibrationResults({ response }) {
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
            Apples-to-apples: markets augur models as events. Sorted by absolute model-vs-market gap.
          </div>
        </div>
        <CleanTable rows={result.clean ?? []} />
      </section>

      <MarkFanPanel markFan={markFan} />

      <section className="augur-panel overflow-hidden" aria-label="Surfaced markets">
        <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
          <div className="augur-eyebrow">Surfaced markets (not scored / context only)</div>
          <div className="mt-1 text-xs augur-muted">
            Markets augur has no event concept for. Shown with the market price plus, where one exists, a related (NOT
            equal) augur signal.
          </div>
        </div>
        <SurfacedList rows={result.surfaced ?? []} />
      </section>
    </div>
  );
}

export function CalibrationWorkspace({ bootstrap }) {
  const catalog = bootstrap.calibration ?? null;
  const presets = bootstrap.exogenousPresets ?? [];
  const defaultPresetId = bootstrap.defaultExogenousPresetId;
  const initialPresetId = presets.includes(defaultPresetId) ? defaultPresetId : (presets[0] ?? null);

  const [input, setInput] = useState({ ...CALIBRATION_INPUT_DEFAULTS, presetId: initialPresetId });
  const [response, setResponse] = useState(null);
  const [runError, setRunError] = useState(null);

  const updateInput = (patch) => setInput((previous) => ({ ...previous, ...patch }));

  // The calibration run is fully determined by these four inputs; memoizing keeps the
  // auto-run effect from re-firing on unrelated re-renders (it keys on this request).
  const request = useMemo(
    () => ({
      presetId: input.presetId,
      horizonMonths: input.horizonMonths,
      rollouts: input.rollouts,
      seed: input.seed,
    }),
    [input.presetId, input.horizonMonths, input.rollouts, input.seed]
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
          <CalibrationForm
            input={input}
            catalog={catalog}
            presets={presets}
            defaultPresetId={defaultPresetId}
            onChange={updateInput}
          />

          <div className="min-w-0 space-y-5">
            {runError ? (
              <div className="augur-note-danger p-4 text-sm">Calibration run failed: {runError}</div>
            ) : response ? (
              <CalibrationResults response={response} />
            ) : (
              <RolloutResultsSkeleton />
            )}
          </div>
        </section>
      </div>
    </CurrencyDisplayProvider>
  );
}
