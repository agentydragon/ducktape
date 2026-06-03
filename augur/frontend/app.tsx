import React, { useEffect, useMemo, useState } from "react";
import { MantineProvider, NativeSelect } from "@mantine/core";

import {
  fetchAugurCalibrationInfo,
  fetchAugurCatalog,
  fetchAugurDeployment,
  fetchAugurSettings,
  fetchProductMetricFan,
  fetchProductPortfolio,
  fetchProductRollout,
} from "./client.ts";
import { fmtNumber } from "./lib/format.ts";
import { fmtMetricValue } from "./lib/chart.ts";

import { MetricFanChart } from "./fan_chart.tsx";
import { TerminalDistributionHistogram } from "./histogram.tsx";
import { TerminalMetricTable, TerminalScenarioComparison } from "./metric_table.tsx";
import { SelectedRolloutEventsPanel, EventKindLegend } from "./events_panel.tsx";
import { ScenarioEditor } from "./scenario_editor.tsx";
import { CalibrationWorkspace } from "./calibration.tsx";
import { BudgetWorkspace } from "./budget.tsx";
import { AugurHeader, SharedControls } from "./header.tsx";
import { RolloutResultsSkeleton, StatCardsSkeleton, ProductProjectionLoading } from "./skeleton.tsx";
import { CurrencyDisplayProvider, useVisibleEventKinds, useEventSelection } from "./hooks.ts";
import {
  METRIC_OPTIONS,
  MAX_VARIANTS,
  productInputDefaults,
  productMetricFanRequest,
  scenarioSetToSearch,
  scenarioSetFromSearch,
  makeVariant,
  resolveVariant,
  housingOverrideFromBase,
  defaultVariantLabel,
  scenarioColor,
  rolloutCountFromSearch,
  rolloutCountDefault,
  clampRolloutCount,
  firstSeedFromSearch,
  firstSeedDefault,
  clampFirstSeed,
  modelFromSearch,
  defaultModel,
  horizonMonthsFromSearch,
  horizonMonthsDefault,
  clampHorizonMonths,
  metricScaleFromSearch,
} from "./input_helpers.ts";
import {
  metricFanRows,
  terminalPercentileValue,
  selectedRolloutMetricRows,
  selectedRolloutEvents,
  visibleMetricOptions,
} from "./data_helpers.ts";

// Top-level views. "product" is the default; the active tab is mirrored to the URL `?tab=`
// (omitted for the default), following the same replaceState pattern as the product `?s=` state.
const TABS = [
  { value: "product", label: "Product" },
  { value: "calibration", label: "Calibration" },
  { value: "budget", label: "Budget" },
];
const TAB_VALUES = new Set(TABS.map((tab) => tab.value));
const DEFAULT_TAB = "product";

function tabFromSearch(searchString) {
  const requested = new URLSearchParams(searchString).get("tab");
  return requested && TAB_VALUES.has(requested) ? requested : DEFAULT_TAB;
}

function writeTabToSearch(tab) {
  const params = new URLSearchParams(window.location.search);
  if (tab === DEFAULT_TAB) params.delete("tab");
  else params.set("tab", tab);
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

// The shared rollout count is a top-level concern (both tabs run this many rollouts), so it gets
// its own `?n=` param written by the shell rather than living in either tab's serialized input.
// Omitted when at the default, mirroring the trailing-default trimming of the product `?s=` state.
function writeRolloutCountToSearch(value, bootstrap) {
  const params = new URLSearchParams(window.location.search);
  if (value == null || value === rolloutCountDefault(bootstrap)) params.delete("n");
  else params.set("n", String(value));
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

function writeFirstSeedToSearch(value, bootstrap) {
  const params = new URLSearchParams(window.location.search);
  if (value == null || value === firstSeedDefault(bootstrap)) params.delete("seed");
  else params.set("seed", String(value));
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

// The shared exogenous model is likewise a top-level concern (both tabs run against it), so it
// gets its own `?x=` param. Omitted when at the deployment default, like the `?n=` param above.
function writeExogenousModelToSearch(value, bootstrap) {
  const params = new URLSearchParams(window.location.search);
  if (value == null || value === defaultModel(bootstrap)) params.delete("x");
  else params.set("x", value);
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

// The shared horizon (`?h=`) and chart scale (`?scale=`) are also top-level, owned by the shell.
function writeHorizonMonthsToSearch(value, bootstrap) {
  const params = new URLSearchParams(window.location.search);
  if (value == null || value === horizonMonthsDefault(bootstrap)) params.delete("h");
  else params.set("h", String(value));
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

function writeMetricScaleToSearch(value) {
  const params = new URLSearchParams(window.location.search);
  if (value === "log") params.set("scale", "log");
  else params.delete("scale");
  const search = params.toString();
  const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
  if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
    window.history.replaceState(null, "", newUrl);
  }
}

function AugurTabBar({ tab, onSelectTab }) {
  return (
    <nav className="flex items-center gap-1" aria-label="Augur views" data-augur-tab-bar="">
      {TABS.map((entry) => (
        <button
          key={entry.value}
          type="button"
          className="augur-view-tab"
          data-active={tab === entry.value ? "" : undefined}
          aria-current={tab === entry.value ? "page" : undefined}
          data-augur-tab={entry.value}
          onClick={() => onSelectTab(entry.value)}
        >
          {entry.label}
        </button>
      ))}
    </nav>
  );
}

function RolloutResultsPanel({
  visibleMetrics,
  selectedMetric,
  onSelectMetric,
  metricScale,
  rolloutSummaries,
  selectedSeed,
  onSelectSeed,
  selectedRolloutLoading,
  fanSeries,
  percentiles,
  scenarios,
  resultsById,
  activeId,
  selectedRows,
  selectedEvents,
  selectedSummary,
  visibleEventKinds,
  eventSelection,
  rolloutError,
}) {
  return (
    <section className="augur-panel overflow-hidden" aria-label="Cash projection workspace">
      <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <NativeSelect
          aria-label="Metric to plot"
          value={selectedMetric.value}
          data={visibleMetrics.map((metric) => ({ value: metric.value, label: metric.label }))}
          classNames={{ input: "augur-tabular min-w-[12rem]" }}
          onChange={(event) => onSelectMetric(event.target.value)}
        />
      </div>
      <TerminalDistributionHistogram
        summaries={rolloutSummaries}
        metric={selectedMetric}
        metricScale={metricScale}
        selectedSeed={selectedSeed}
        loadingSeed={selectedRolloutLoading ? selectedSeed : null}
        onSelect={onSelectSeed}
      />
      {fanSeries.some((entry) => entry.rows.length > 0) ? (
        <div className="relative">
          <MetricFanChart
            series={fanSeries}
            metric={selectedMetric}
            metricScale={metricScale}
            percentiles={percentiles}
            selectedRows={selectedRows}
            selectedEvents={selectedEvents}
            selectedSeed={selectedSeed}
            selectedFailed={selectedSummary?.failed ?? false}
            visibleEventKinds={visibleEventKinds.visible}
            selectedEventMonthIndex={eventSelection.selectedEventMonthIndex}
            hoveredEventMonthIndex={eventSelection.hoveredEventMonthIndex}
            onSelectEventMonth={eventSelection.onSelectEventMonth}
            onHoverEventMonth={eventSelection.onHoverEventMonth}
          />
          {selectedRolloutLoading && (
            <div
              className="pointer-events-none absolute inset-0 flex items-center justify-center bg-white/40 backdrop-blur-[1px] dark:bg-slate-950/40"
              data-product-selected-rollout-loading=""
            >
              <span className="inline-flex items-center gap-2 rounded-full bg-white/90 px-3 py-1 text-xs font-medium text-slate-600 shadow-sm dark:bg-slate-900/90 dark:text-slate-300">
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-blue-600 dark:border-slate-600 dark:border-t-blue-400" />
                Loading rollout {selectedSeed}…
              </span>
            </div>
          )}
        </div>
      ) : (
        <div className="flex min-h-[22rem] items-center justify-center text-sm augur-muted">Running...</div>
      )}
      <TerminalScenarioComparison
        scenarios={scenarios}
        resultsById={resultsById}
        metrics={visibleMetrics}
        activeId={activeId}
      />
      {selectedSeed != null && selectedEvents.length > 0 && (
        <EventKindLegend events={selectedEvents} visibility={visibleEventKinds} />
      )}
      {rolloutError && (
        <div className="border-t border-slate-200 p-4 dark:border-slate-700">
          <div className="augur-note-danger">Selected rollout failed to load: {rolloutError}</div>
        </div>
      )}
      {selectedSeed != null && (
        <SelectedRolloutEventsPanel
          events={selectedEvents}
          selectedSummary={selectedSummary}
          loading={selectedRolloutLoading}
          selectedEventMonthIndex={eventSelection.selectedEventMonthIndex}
          hoveredEventMonthIndex={eventSelection.hoveredEventMonthIndex}
          onSelectEventMonth={eventSelection.onSelectEventMonth}
          onHoverEventMonth={eventSelection.onHoverEventMonth}
        />
      )}
      <TerminalMetricTable
        summaries={rolloutSummaries}
        selectedSummary={selectedSummary}
        metrics={visibleMetrics}
        selectedMetric={selectedMetric}
      />
    </section>
  );
}

function shortCommit(commit) {
  if (!commit) return null;
  return commit.slice(0, 12);
}

function DeploymentCommitItem({ label, image }) {
  const commit = shortCommit(image?.sourceCommit);
  if (!commit) return null;
  const className = "mono font-semibold text-slate-700 dark:text-slate-200";
  const title = image?.imageTag ? `${label}: ${image.imageTag}` : label;
  return (
    <span className="whitespace-nowrap" title={title}>
      {label}{" "}
      {image.sourceCommitUrl ? (
        <a className={`${className} augur-link`} href={image.sourceCommitUrl}>
          {commit}
        </a>
      ) : (
        <span className={className}>{commit}</span>
      )}
    </span>
  );
}

function DeploymentCommitSummary({ deployment }) {
  const apiCommit = deployment?.api?.sourceCommit ?? null;
  const frontendCommit = deployment?.frontend?.sourceCommit ?? null;
  if (!apiCommit && !frontendCommit) return null;
  if (apiCommit && frontendCommit && apiCommit === frontendCommit) {
    return <DeploymentCommitItem label="Deployed" image={deployment.api} />;
  }
  return (
    <>
      <DeploymentCommitItem label="API" image={deployment?.api} />
      <DeploymentCommitItem label="UI" image={deployment?.frontend} />
    </>
  );
}

function currencyDisplayFromSearch(searchString) {
  return new URLSearchParams(searchString).get("fmt") === "exact" ? "exact" : "compact";
}

function ProductProjectionWorkspace({
  bootstrap,
  deployment,
  tab,
  onSelectTab,
  rolloutCount,
  onChangeRolloutCount,
  firstSeed,
  onChangeFirstSeed,
  model,
  onChangeModel,
  horizonMonths,
  onChangeHorizonMonths,
  metricScale,
  onChangeMetricScale,
  currencyDisplay,
  onChangeCurrencyDisplay,
  settingsOpen,
  onChangeSettingsOpen,
}) {
  const [scenarioSet, setScenarioSet] = useState(() => scenarioSetFromSearch(window.location.search, bootstrap));
  const [selectedMetricValue, setSelectedMetricValue] = useState("net_worth_usd");
  // One metric-fan response per scenario id. Every scenario shares the seed set, and identical
  // seeds reproduce identical sampled exogenous paths, so the overlaid fans are apples-to-apples
  // (no backend comparison endpoint needed). Updated in place as each fan arrives so the comparison
  // fans don't blank out while the active scenario is being edited.
  const [resultsById, setResultsById] = useState(() => new Map());
  const [errorsById, setErrorsById] = useState(() => new Map());
  const [portfolio, setPortfolio] = useState(null);
  const [portfolioError, setPortfolioError] = useState(null);
  const [selectedSeed, setSelectedSeed] = useState(null);
  const [rolloutDetails, setRolloutDetails] = useState(() => new Map());
  const [rolloutError, setRolloutError] = useState(null);
  const eventSelection = useEventSelection();
  const visibleEventKinds = useVisibleEventKinds();

  const { base, variants, activeId } = scenarioSet;
  // The chart's scenario set: the always-present Base (series 0) plus each variant resolved against
  // it (`{ ...base, ...overrides }`). Variants inherit every knob they don't override, so editing a
  // Base knob propagates to inheriting variants for free. The resolved list is exactly the
  // `{ id, label, input }[]` shape every downstream consumer (requests, fans, comparison table,
  // chips) already expects, so the base+overrides model stays contained to the editor + this memo.
  const chartScenarios = useMemo(
    () => [
      { id: "base", label: base.label, input: base.input },
      ...variants.map((variant) => ({
        id: variant.id,
        label: variant.label,
        input: resolveVariant(base.input, variant.overrides),
      })),
    ],
    [base, variants]
  );
  // Metric list is the union across scenarios: a metric stays offered as long as *some* scenario
  // surfaces it (e.g. "Property value" once any scenario buys), so a comparison never hides a column.
  const visibleMetrics = useMemo(() => {
    const visibleValues = new Set();
    for (const entry of chartScenarios)
      for (const metric of visibleMetricOptions(entry.input)) visibleValues.add(metric.value);
    return METRIC_OPTIONS.filter((metric) => visibleValues.has(metric.value));
  }, [chartScenarios]);
  const selectedMetric =
    visibleMetrics.find((metric) => metric.value === selectedMetricValue) ?? visibleMetrics[0] ?? METRIC_OPTIONS[0];

  const requestEntries = useMemo(
    () =>
      chartScenarios.map((entry) => ({
        id: entry.id,
        request: productMetricFanRequest(entry.input, bootstrap, selectedMetric, {
          rolloutCount,
          firstSeed,
          model,
          horizonMonths,
        }),
      })),
    [chartScenarios, bootstrap, selectedMetric, rolloutCount, firstSeed, model, horizonMonths]
  );
  const activeRequest = requestEntries.find((entry) => entry.id === activeId)?.request ?? requestEntries[0].request;
  const activeResult = resultsById.get(activeId) ?? null;
  const runError = errorsById.get(activeId) ?? null;

  // One overlay series per scenario; Base is series 0 (blue), variants follow. Color is by position
  // so it stays put as the active selection moves. The active scenario also drives the histogram,
  // selected-rollout overlay, and events.
  const fanSeries = useMemo(
    () =>
      chartScenarios.map((entry, index) => ({
        id: entry.id,
        label: entry.label,
        color: scenarioColor(index),
        rows: metricFanRows(resultsById.get(entry.id)),
        isActive: entry.id === activeId,
      })),
    [chartScenarios, resultsById, activeId]
  );

  const scenarioCacheKey = useMemo(() => JSON.stringify(activeRequest.scenario), [activeRequest.scenario]);
  const rolloutSummaries = useMemo(() => activeResult?.rolloutSummaries ?? [], [activeResult]);
  const selectedSummary = useMemo(
    () => rolloutSummaries.find((summary) => Number(summary.seed) === selectedSeed) ?? null,
    [rolloutSummaries, selectedSeed]
  );
  const selectedDetailKey = selectedSeed == null ? null : `${scenarioCacheKey}|${selectedSeed}`;
  const selectedDetail = selectedDetailKey ? rolloutDetails.get(selectedDetailKey) : null;
  const selectedRows = useMemo(
    () => selectedRolloutMetricRows(selectedDetail, selectedMetric),
    [selectedDetail, selectedMetric]
  );
  const selectedEvents = useMemo(() => selectedRolloutEvents(selectedDetail), [selectedDetail]);
  const failedCount = activeResult?.failedCount ?? null;
  const terminalP50 = terminalPercentileValue(activeResult, 50);
  const selectedRolloutLoading = selectedSeed != null && activeResult != null && !selectedDetail && !rolloutError;

  // -- Base + variant operations. Base edits propagate to every variant that doesn't override the
  // touched knob (variants resolve as `{ ...base, ...overrides }`); variant edits write to that
  // variant's `overrides` only. ----------------------------------------------------------------
  const updateBaseInput = (patch) =>
    setScenarioSet((previous) => ({
      ...previous,
      base: { ...previous.base, input: { ...previous.base.input, ...patch } },
    }));
  const setBaseField = (key, value) => updateBaseInput({ [key]: value });
  const resetBase = () =>
    setScenarioSet((previous) => ({ ...previous, base: { ...previous.base, input: productInputDefaults(bootstrap) } }));
  const selectEntry = (id) => setScenarioSet((previous) => ({ ...previous, activeId: id }));
  const renameEntry = (id, label) =>
    setScenarioSet((previous) =>
      id === "base"
        ? { ...previous, base: { ...previous.base, label } }
        : { ...previous, variants: previous.variants.map((v) => (v.id === id ? { ...v, label } : v)) }
    );
  // Merge a partial-override patch into one variant's `overrides` (scalar cells + the variant
  // housing panel). Overridden keys win over the inherited base values.
  const patchVariantOverrides = (id, patch) =>
    setScenarioSet((previous) => ({
      ...previous,
      variants: previous.variants.map((v) => (v.id === id ? { ...v, overrides: { ...v.overrides, ...patch } } : v)),
    }));
  // Drop override keys so the variant re-inherits the base value(s): a scalar cell's "revert" or the
  // whole housing cluster's "revert to base".
  const revertVariantKeys = (id, keys) =>
    setScenarioSet((previous) => ({
      ...previous,
      variants: previous.variants.map((v) => {
        if (v.id !== id) return v;
        const overrides = { ...v.overrides };
        for (const key of keys) delete overrides[key];
        return { ...v, overrides };
      }),
    }));
  // Pin the housing cluster on a variant by copying the base's housing as a unit; later base housing
  // edits no longer reach it until reverted.
  const overrideHousing = (id) =>
    setScenarioSet((previous) => ({
      ...previous,
      variants: previous.variants.map((v) =>
        v.id === id ? { ...v, overrides: { ...v.overrides, ...housingOverrideFromBase(previous.base.input) } } : v
      ),
    }));
  const addVariant = () =>
    setScenarioSet((previous) => {
      if (previous.variants.length >= MAX_VARIANTS) return previous;
      // A fresh variant overrides nothing: it starts identical to Base, then the user overrides the
      // knobs that differ (rent vs. buy, a higher spend, …).
      const variant = makeVariant(defaultVariantLabel(previous.variants.length));
      return { ...previous, variants: [...previous.variants, variant], activeId: variant.id };
    });
  const deleteVariant = (id) =>
    setScenarioSet((previous) => ({
      ...previous,
      variants: previous.variants.filter((v) => v.id !== id),
      activeId: previous.activeId === id ? "base" : previous.activeId,
    }));

  useEffect(() => {
    // The active selection is ephemeral UI state, not persisted: the codec always decodes Base as
    // active, so reloading a shared link lands on Base.
    const params = new URLSearchParams(scenarioSetToSearch(base, variants, bootstrap));
    if (currencyDisplay !== "compact") params.set("fmt", "exact");
    // The shell-owned shared params live outside the scenario set, as do the budget tab's planning
    // params (`bhide`/`bset`). Carry whichever are currently set across so rewriting the product
    // `?s=`/`?scenarios=` state doesn't drop them when switching away from and back to another tab.
    const currentParams = new URLSearchParams(window.location.search);
    for (const key of ["n", "seed", "x", "h", "scale", "fmt", "bhide", "bset"]) {
      const value = currentParams.get(key);
      if (value != null) params.set(key, value);
    }
    const search = params.toString();
    const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
    if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
      window.history.replaceState(null, "", newUrl);
    }
  }, [base, variants, bootstrap, currencyDisplay]);

  useEffect(() => {
    const controller = new AbortController();
    fetchProductPortfolio({ signal: controller.signal })
      .then((payload) => {
        setPortfolio(payload);
        setPortfolioError(null);
      })
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setPortfolio(null);
        setPortfolioError(error?.message || String(error));
      });
    return () => controller.abort();
  }, []);

  // Drop cached fans/errors for scenarios that no longer exist (deleted from the set).
  useEffect(() => {
    const ids = new Set(chartScenarios.map((entry) => entry.id));
    const prune = (previous) => {
      const next = new Map([...previous].filter(([id]) => ids.has(id)));
      return next.size === previous.size ? previous : next;
    };
    setResultsById(prune);
    setErrorsById(prune);
  }, [chartScenarios]);

  useEffect(() => {
    const controller = new AbortController();
    // Fan out one request per scenario over the shared seed set. Results land in place (no clear)
    // so the comparison fans stay put while the active scenario is being edited; unchanged
    // scenarios re-request the same key and return from the server's rollout cache.
    const handle = setTimeout(() => {
      for (const { id, request } of requestEntries) {
        fetchProductMetricFan(request, { signal: controller.signal })
          .then((payload) => {
            setResultsById((previous) => new Map(previous).set(id, payload));
            setErrorsById((previous) => {
              if (!previous.has(id)) return previous;
              const next = new Map(previous);
              next.delete(id);
              return next;
            });
          })
          .catch((error) => {
            if (error?.name === "AbortError") return;
            setErrorsById((previous) => new Map(previous).set(id, error?.message || String(error)));
          });
      }
    }, 120);
    return () => {
      clearTimeout(handle);
      controller.abort();
    };
  }, [requestEntries]);

  useEffect(() => {
    if (selectedSeed == null || !activeResult?.rolloutSummaries) return;
    if (!activeResult.rolloutSummaries.some((summary) => Number(summary.seed) === selectedSeed)) {
      setSelectedSeed(null);
    }
  }, [activeResult, selectedSeed]);

  useEffect(() => {
    eventSelection.clear();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDetailKey]);

  useEffect(() => {
    if (selectedSeed == null || activeResult == null || selectedDetailKey == null) return;
    if (rolloutDetails.has(selectedDetailKey)) return;
    const controller = new AbortController();
    setRolloutError(null);
    fetchProductRollout({ scenario: activeRequest.scenario, seed: selectedSeed }, { signal: controller.signal })
      .then((payload) => {
        setRolloutDetails((previous) => {
          const next = new Map(previous);
          next.set(selectedDetailKey, payload);
          return next;
        });
      })
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setRolloutError(error?.message || String(error));
      });
    return () => controller.abort();
  }, [activeRequest.scenario, activeResult, rolloutDetails, selectedDetailKey, selectedSeed]);

  return (
    <div
      data-augur-surface="product"
      className="min-h-screen bg-slate-100 text-slate-800 dark:bg-slate-950 dark:text-slate-100"
    >
      <AugurHeader
        nav={<AugurTabBar tab={tab} onSelectTab={onSelectTab} />}
        rightSlot={<DeploymentCommitSummary deployment={deployment} />}
      />

      <main className="space-y-5 px-4 py-6 sm:px-6 lg:px-8">
        <SharedControls
          rolloutCount={rolloutCount}
          onChangeRolloutCount={onChangeRolloutCount}
          maxRolloutCount={bootstrap.maxRolloutSamples}
          firstSeed={firstSeed}
          onChangeFirstSeed={onChangeFirstSeed}
          model={model}
          onChangeModel={onChangeModel}
          models={bootstrap.models}
          horizonMonths={horizonMonths}
          onChangeHorizonMonths={onChangeHorizonMonths}
          maxHorizonMonths={bootstrap.maxHorizonMonths}
          metricScale={metricScale}
          onChangeMetricScale={onChangeMetricScale}
          currencyDisplay={currencyDisplay}
          onChangeCurrencyDisplay={onChangeCurrencyDisplay}
          settingsOpen={settingsOpen}
          onChangeSettingsOpen={onChangeSettingsOpen}
        />

        <ScenarioEditor
          base={base}
          variants={variants}
          activeId={activeId}
          bootstrap={bootstrap}
          portfolio={portfolio}
          portfolioError={portfolioError}
          horizonMonths={horizonMonths}
          onSelect={selectEntry}
          onAddVariant={addVariant}
          onDeleteVariant={deleteVariant}
          onRename={renameEntry}
          onResetBase={resetBase}
          onSetBaseField={setBaseField}
          onSetBasePatch={updateBaseInput}
          onPatchVariant={patchVariantOverrides}
          onRevertKeys={revertVariantKeys}
          onOverrideHousing={overrideHousing}
        />

        {runError && <div className="augur-note-danger p-4 text-sm">Product projection failed: {runError}</div>}

        {activeResult ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="augur-card p-4">
              <div className="augur-eyebrow">Median terminal {selectedMetric.label.toLowerCase()}</div>
              <div className="mt-2 text-2xl font-semibold augur-tabular">
                {fmtMetricValue(selectedMetric.chartValue, terminalP50, currencyDisplay)}
              </div>
            </div>
            <div className="augur-card p-4">
              <div className="augur-eyebrow">Failed rollouts</div>
              <div className="mt-2 text-2xl font-semibold augur-tabular">
                {fmtNumber(failedCount)} / {fmtNumber(activeRequest.rolloutSeeds.length)}
              </div>
            </div>
          </div>
        ) : (
          <StatCardsSkeleton />
        )}

        {activeResult ? (
          <RolloutResultsPanel
            visibleMetrics={visibleMetrics}
            selectedMetric={selectedMetric}
            onSelectMetric={setSelectedMetricValue}
            metricScale={metricScale}
            rolloutSummaries={rolloutSummaries}
            selectedSeed={selectedSeed}
            onSelectSeed={setSelectedSeed}
            selectedRolloutLoading={selectedRolloutLoading}
            fanSeries={fanSeries}
            percentiles={activeRequest.percentiles}
            scenarios={chartScenarios}
            resultsById={resultsById}
            activeId={activeId}
            selectedRows={selectedRows}
            selectedEvents={selectedEvents}
            selectedSummary={selectedSummary}
            visibleEventKinds={visibleEventKinds}
            eventSelection={eventSelection}
            rolloutError={rolloutError}
          />
        ) : (
          <RolloutResultsSkeleton />
        )}
      </main>
    </div>
  );
}

function BudgetAppSurface({ deployment, tab, onSelectTab }) {
  return (
    <div
      data-augur-surface="budget"
      className="min-h-screen bg-slate-100 text-slate-800 dark:bg-slate-950 dark:text-slate-100"
    >
      <AugurHeader
        nav={<AugurTabBar tab={tab} onSelectTab={onSelectTab} />}
        rightSlot={<DeploymentCommitSummary deployment={deployment} />}
      />
      <BudgetWorkspace />
    </div>
  );
}

function CalibrationAppSurface({
  bootstrap,
  deployment,
  tab,
  onSelectTab,
  rolloutCount,
  onChangeRolloutCount,
  firstSeed,
  onChangeFirstSeed,
  model,
  onChangeModel,
  horizonMonths,
  onChangeHorizonMonths,
  metricScale,
  onChangeMetricScale,
  currencyDisplay,
  onChangeCurrencyDisplay,
  settingsOpen,
  onChangeSettingsOpen,
}) {
  return (
    <div
      data-augur-surface="calibration"
      className="min-h-screen bg-slate-100 text-slate-800 dark:bg-slate-950 dark:text-slate-100"
    >
      <AugurHeader
        nav={<AugurTabBar tab={tab} onSelectTab={onSelectTab} />}
        rightSlot={<DeploymentCommitSummary deployment={deployment} />}
      />
      <main className="px-4 py-6 sm:px-6 lg:px-8">
        <CalibrationWorkspace
          bootstrap={bootstrap}
          rolloutCount={rolloutCount}
          firstSeed={firstSeed}
          model={model}
          horizonMonths={horizonMonths}
          metricScale={metricScale}
          sharedControlsSlot={
            <SharedControls
              rolloutCount={rolloutCount}
              onChangeRolloutCount={onChangeRolloutCount}
              maxRolloutCount={bootstrap.maxRolloutSamples}
              firstSeed={firstSeed}
              onChangeFirstSeed={onChangeFirstSeed}
              model={model}
              onChangeModel={onChangeModel}
              models={bootstrap.models}
              horizonMonths={horizonMonths}
              onChangeHorizonMonths={onChangeHorizonMonths}
              maxHorizonMonths={bootstrap.maxHorizonMonths}
              metricScale={metricScale}
              onChangeMetricScale={onChangeMetricScale}
              currencyDisplay={currencyDisplay}
              onChangeCurrencyDisplay={onChangeCurrencyDisplay}
              settingsOpen={settingsOpen}
              onChangeSettingsOpen={onChangeSettingsOpen}
            />
          }
        />
      </main>
    </div>
  );
}

// Mounted once bootstrap has loaded so the shared defaults (which depend on bootstrap fields like
// `maxRolloutSamples` and `models`) are available at state-init time. Owns the cross-tab
// state — the active tab plus shared controls — and hands it to whichever workspace is active.
function LoadedAppShell({ bootstrap, deployment }) {
  const [tab, setTab] = useState(() => tabFromSearch(window.location.search));
  const [rolloutCount, setRolloutCount] = useState(() => rolloutCountFromSearch(window.location.search, bootstrap));
  const [firstSeed, setFirstSeed] = useState(() => firstSeedFromSearch(window.location.search, bootstrap));
  const [model, setModel] = useState(() => modelFromSearch(window.location.search, bootstrap));
  const [horizonMonths, setHorizonMonths] = useState(() => horizonMonthsFromSearch(window.location.search, bootstrap));
  const [metricScale, setMetricScale] = useState(() => metricScaleFromSearch(window.location.search));
  const [currencyDisplay, setCurrencyDisplay] = useState(() => currencyDisplayFromSearch(window.location.search));
  const [settingsOpen, setSettingsOpen] = useState(true);

  const onSelectTab = (next) => {
    setTab(next);
    writeTabToSearch(next);
  };

  const onChangeRolloutCount = (value) => {
    const next = value == null ? value : clampRolloutCount(value, bootstrap);
    setRolloutCount(next);
    writeRolloutCountToSearch(next, bootstrap);
  };

  const onChangeFirstSeed = (value) => {
    const next = value == null ? value : clampFirstSeed(value);
    setFirstSeed(next);
    writeFirstSeedToSearch(next, bootstrap);
  };

  const onChangeModel = (value) => {
    setModel(value);
    writeExogenousModelToSearch(value, bootstrap);
  };

  const onChangeHorizonMonths = (value) => {
    const next = value == null ? value : clampHorizonMonths(value, bootstrap);
    setHorizonMonths(next);
    writeHorizonMonthsToSearch(next, bootstrap);
  };

  const onChangeMetricScale = (value) => {
    setMetricScale(value);
    writeMetricScaleToSearch(value);
  };

  const onChangeCurrencyDisplay = (value) => {
    setCurrencyDisplay(value);
    const params = new URLSearchParams(window.location.search);
    if (value !== "compact") params.set("fmt", "exact");
    else params.delete("fmt");
    const search = params.toString();
    const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
    window.history.replaceState(null, "", newUrl);
  };

  const sharedProps = {
    bootstrap,
    deployment,
    tab,
    onSelectTab,
    rolloutCount,
    onChangeRolloutCount,
    firstSeed,
    onChangeFirstSeed,
    model,
    onChangeModel,
    horizonMonths,
    onChangeHorizonMonths,
    metricScale,
    onChangeMetricScale,
    currencyDisplay,
    onChangeCurrencyDisplay,
    settingsOpen,
    onChangeSettingsOpen: setSettingsOpen,
  };
  let surface;
  if (tab === "calibration") {
    surface = <CalibrationAppSurface {...sharedProps} />;
  } else if (tab === "budget") {
    surface = <BudgetAppSurface {...sharedProps} />;
  } else {
    surface = <ProductProjectionWorkspace {...sharedProps} />;
  }
  return (
    <CurrencyDisplayProvider value={{ display: currencyDisplay, setDisplay: onChangeCurrencyDisplay }}>
      {surface}
    </CurrencyDisplayProvider>
  );
}

function ProductProjectionAppShell() {
  const [bootstrap, setBootstrap] = useState(null);
  const [bootstrapError, setBootstrapError] = useState(null);
  const [deployment, setDeployment] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    // The bootstrap endpoint is split into cohesive resources (`/api/catalog`, `/api/settings`,
    // `/api/calibration`). The shell needs catalog + settings together to mount (shared-control
    // defaults read off both), so fetch them in parallel and merge back into the single view
    // model the workspaces consume.
    Promise.all([
      fetchAugurCatalog({ signal: controller.signal }),
      fetchAugurSettings({ signal: controller.signal }),
      fetchAugurCalibrationInfo({ signal: controller.signal }),
    ])
      .then(([catalog, settings, calibration]) => {
        setBootstrap({ ...catalog, ...settings, calibration });
        setBootstrapError(null);
      })
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setBootstrap(null);
        setBootstrapError(error?.message || String(error));
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetchAugurDeployment({ signal: controller.signal })
      .then((payload) => setDeployment(payload))
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setDeployment(null);
      });
    return () => controller.abort();
  }, []);

  if (!bootstrap) return <ProductProjectionLoading error={bootstrapError} />;

  return <LoadedAppShell bootstrap={bootstrap} deployment={deployment} />;
}

export default function AugurApp() {
  return (
    <MantineProvider defaultColorScheme="auto">
      <ProductProjectionAppShell />
    </MantineProvider>
  );
}
