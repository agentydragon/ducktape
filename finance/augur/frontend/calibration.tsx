import React, { useEffect, useMemo, useState } from "react";

import { CategoricalPanel } from "./categorical_chart.tsx";
import { CleanTable, SurfacedTable } from "./calibration_tables.tsx";
import { fetchCalibrationRun } from "./client.ts";
import { toastFetchError } from "./lib/toast.ts";
import { MetricFanChart } from "./fan_chart.tsx";
import { RolloutResultsSkeleton } from "./skeleton.tsx";
import { SanityBandsPanel } from "./sanity_bands_panel.tsx";
import { FAN_PERCENTILES, clampRolloutCount, clampFirstSeed } from "./input_helpers.ts";
import { markFanRows } from "./data_helpers.ts";

// `chartValue` ending in `Usd` makes the shared `MetricFanChart` axis/tooltip format these
// issuer channels as currency.
const MARK_METRIC = { value: "mark_usd_per_unit", chartValue: "markUsd", label: "Per-unit mark" };
const VALUATION_METRIC = {
  value: "company_valuation_usd",
  chartValue: "companyValuationUsd",
  label: "Company valuation",
};

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
          description={`Percentile bands of ${fan.issuer}'s modelled per-unit mark over the full horizon.`}
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
          description={`Percentile bands of ${fan.issuer}'s modelled company valuation over the full horizon.`}
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

export function CalibrationWorkspace({ bootstrap, rolloutCount, firstSeed, model, metricScale, sharedControlsSlot }) {
  const catalog = bootstrap.calibration ?? null;

  const [response, setResponse] = useState(null);
  const [runError, setRunError] = useState(null);
  // A run is in flight over already-rendered results (vs. the first load, which shows the skeleton).
  // Drives a subtle dim instead of tearing the page down — see the effect and render below.
  const [refreshing, setRefreshing] = useState(false);

  // The calibration run is determined by the tab-shared exogenous model (`?x=`) and rollout count
  // (`?n=`), plus the fixed first seed. It always resolves markets at the deployment's full max
  // rollout horizon — deliberately independent of the product tab's `?h=` zoom — so a fan-chart
  // scroll never changes the scored markets or re-runs the catalog. Memoizing keeps the auto-run
  // effect from re-firing on unrelated re-renders (it keys on this request).
  const rollouts = clampRolloutCount(rolloutCount, bootstrap);
  const seed = clampFirstSeed(firstSeed);
  const horizonMonths = bootstrap.maxHorizonMonths;
  const request = useMemo(
    () => ({
      presetId: model,
      horizonMonths,
      rollouts,
      seed,
    }),
    [model, horizonMonths, rollouts, seed]
  );

  // Live auto-run (no button): debounce input changes, abort the in-flight run, and re-score
  // on every settled request — mirrors the product page's metric-fan auto-refresh.
  useEffect(() => {
    if (!catalog || !request.presetId) return undefined;
    const controller = new AbortController();
    // Stale-while-revalidate: keep the current results on screen while the new run loads instead of
    // blanking to the skeleton. Changing the model (`?x=`) or rollout count (`?n=`) shouldn't flash
    // the scored / surfaced market tables away. Only the first load (no `response` yet) falls through
    // to the skeleton. Clear any stale error so a prior failure doesn't shadow the retry.
    setRunError(null);
    setRefreshing(true);
    const handle = setTimeout(() => {
      fetchCalibrationRun(request, { signal: controller.signal })
        .then((payload) => {
          setResponse(payload);
          setRunError(null);
        })
        .catch((error) => {
          if (error?.name === "AbortError") return;
          setRunError(error?.message || String(error));
          toastFetchError("calibration-run", "Calibration run failed", error);
        })
        .finally(() => {
          // A superseded run's abort lands here too; the run that replaced it now owns `refreshing`.
          if (!controller.signal.aborted) setRefreshing(false);
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
            <div className={`transition-opacity ${refreshing ? "opacity-60" : ""}`} aria-busy={refreshing}>
              <CalibrationResults response={response} metricScale={metricScale} />
            </div>
          ) : (
            <RolloutResultsSkeleton />
          )}
        </div>
      </section>
    </div>
  );
}
