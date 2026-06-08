import React, { useEffect, useMemo, useState } from "react";
import { NativeSelect, SegmentedControl } from "@mantine/core";

import {
  fetchProductMetricFan,
  fetchProductPortfolio,
  fetchProductRollout,
  fetchProductTerminalDistribution,
} from "./client.ts";
import { fmtNumber } from "./lib/format.ts";
import { fmtMetricValue } from "./lib/chart.ts";
import { toastFetchError } from "./lib/toast.ts";

import { MetricFanChart } from "./fan_chart.tsx";
import { TerminalDistributionChart } from "./terminal_distribution.tsx";
import { TerminalMetricTable, TerminalScenarioComparison } from "./metric_table.tsx";
import { SelectedRolloutEventsPanel, EventKindLegend } from "./events_panel.tsx";
import { ScenarioEditor } from "./scenario_editor.tsx";
import { ScenarioBadge } from "./scenario_tabs.tsx";
import { AugurHeader, SharedControls, AugurTabBar, DeploymentCommitSummary } from "./header.tsx";
import { RolloutResultsSkeleton, StatCardsSkeleton } from "./skeleton.tsx";
import { useVisibleEventKinds, useEventSelection } from "./hooks.ts";
import {
  METRIC_OPTIONS,
  MAX_VARIANTS,
  productInputDefaults,
  productMetricFanRequest,
  productTerminalDistributionRequest,
  scenarioSetToSearch,
  scenarioSetFromSearch,
  makeVariant,
  resolveVariant,
  defaultVariantLabel,
  scenarioColor,
} from "./input_helpers.ts";
import {
  metricFanRows,
  terminalPercentileValue,
  terminalSampleAtPercentile,
  selectedRolloutMetricRows,
  selectedRolloutEvents,
  visibleMetricOptions,
} from "./data_helpers.ts";

function RolloutResultsPanel({
  visibleMetrics,
  selectedMetric,
  onSelectMetric,
  metricScale,
  horizonMonths,
  onChangeHorizonMonths,
  maxHorizonMonths,
  selectedSeed,
  selectedPercentile,
  onSelectPercentile,
  onClearSelection,
  selectedRolloutLoading,
  fanSeries,
  percentiles,
  scenarios,
  terminalResultsById,
  activeId,
  activeTerminalResult,
  selectedRows,
  selectedEvents,
  selectedSummary,
  visibleEventKinds,
  eventSelection,
  rolloutError,
}) {
  const [chartMode, setChartMode] = useState("fan");
  const [candleBucketMonths, setCandleBucketMonths] = useState(6);
  // "Focus" collapses the overlay to just the active scenario, which the chart then renders as a
  // full single-scenario fan (inner+outer bands, edge lines) — the pre-comparison view, on demand,
  // for reading one variant's timeline without the other scenarios' noise. The fan/candle chart and
  // the distribution chart overlay every scenario; the selected rollout and tables scope to active.
  const [focusActive, setFocusActive] = useState(false);
  const multipleScenarios = scenarios.length > 1;
  const activeScenario = fanSeries.find((entry) => entry.isActive) ?? fanSeries[0];
  const chartSeries = focusActive && multipleScenarios ? fanSeries.filter((entry) => entry.isActive) : fanSeries;
  return (
    <section className="augur-panel overflow-hidden" aria-label="Cash projection workspace">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="flex items-center gap-3">
          <NativeSelect
            aria-label="Metric to plot"
            value={selectedMetric.value}
            data={visibleMetrics.map((metric) => ({ value: metric.value, label: metric.label }))}
            classNames={{ input: "augur-tabular min-w-[12rem]" }}
            onChange={(event) => onSelectMetric(event.target.value)}
          />
          {multipleScenarios && (
            <span className="inline-flex items-center gap-1.5">
              <span className="augur-eyebrow">Active</span>
              <ScenarioBadge label={activeScenario.label} color={activeScenario.color} />
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {multipleScenarios && (
            <SegmentedControl
              size="xs"
              aria-label="Scenario focus"
              value={focusActive ? "focus" : "compare"}
              data={[
                { value: "compare", label: "Compare" },
                { value: "focus", label: "Focus" },
              ]}
              onChange={(value) => setFocusActive(value === "focus")}
              data-product-scenario-focus-toggle=""
            />
          )}
          <SegmentedControl
            size="xs"
            aria-label="Chart style"
            value={chartMode}
            data={[
              { value: "fan", label: "Fans" },
              { value: "candles", label: "Candles" },
            ]}
            onChange={setChartMode}
            data-product-chart-mode-toggle=""
          />
          {chartMode === "candles" && (
            <SegmentedControl
              size="xs"
              aria-label="Candle width"
              value={String(candleBucketMonths)}
              data={[
                { value: "1", label: "1mo" },
                { value: "3", label: "3mo" },
                { value: "6", label: "6mo" },
              ]}
              onChange={(value) => setCandleBucketMonths(Number(value))}
              data-product-candle-width-toggle=""
            />
          )}
        </div>
      </div>
      <TerminalDistributionChart
        scenarios={scenarios}
        resultsById={terminalResultsById}
        activeId={activeId}
        metric={selectedMetric}
        metricScale={metricScale}
        selectedPercentile={selectedPercentile}
        loadingPercentile={selectedRolloutLoading ? selectedPercentile : null}
        onSelectPercentile={onSelectPercentile}
        onClear={onClearSelection}
      />
      {fanSeries.some((entry) => entry.rows.length > 0) ? (
        <div className="relative">
          <MetricFanChart
            series={chartSeries}
            metric={selectedMetric}
            metricScale={metricScale}
            horizonMonths={horizonMonths}
            onChangeHorizonMonths={onChangeHorizonMonths}
            maxHorizonMonths={maxHorizonMonths}
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
            mode={chartMode}
            candleBucketMonths={candleBucketMonths}
          />
          {selectedRolloutLoading && (
            <div
              className="pointer-events-none absolute inset-0 flex items-center justify-center bg-white/40 backdrop-blur-[1px] dark:bg-slate-950/40"
              data-product-selected-rollout-loading=""
            >
              <span className="inline-flex items-center gap-2 rounded-full bg-white/90 px-3 py-1 text-xs font-medium text-slate-600 shadow-sm dark:bg-slate-900/90 dark:text-slate-300">
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-blue-600 dark:border-slate-600 dark:border-t-blue-400" />
                Loading rollout{selectedSeed == null ? "" : ` ${selectedSeed}`}...
              </span>
            </div>
          )}
        </div>
      ) : (
        <div className="flex min-h-[22rem] items-center justify-center text-sm augur-muted">Running...</div>
      )}
      <TerminalScenarioComparison
        scenarios={scenarios}
        resultsById={terminalResultsById}
        metric={selectedMetric}
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
          activeScenario={multipleScenarios ? activeScenario : null}
          loading={selectedRolloutLoading}
          selectedEventMonthIndex={eventSelection.selectedEventMonthIndex}
          hoveredEventMonthIndex={eventSelection.hoveredEventMonthIndex}
          onSelectEventMonth={eventSelection.onSelectEventMonth}
          onHoverEventMonth={eventSelection.onHoverEventMonth}
        />
      )}
      <TerminalMetricTable
        result={activeTerminalResult}
        selectedSummary={selectedSummary}
        selectedMetric={selectedMetric}
      />
    </section>
  );
}

// The Product surface: owns the comparison scenario set (Base + variants), fans out one metric-fan
// request per scenario over a shared seed set, and renders the editor, stat cards, and rollout
// results. Shared controls (rollout count / seed / model / horizon / scale / currency) are owned by
// the app shell and passed in; this component owns only product-tab state.
export function ProductProjectionWorkspace({
  bootstrap,
  deployment,
  tab,
  onSelectTab,
  rolloutCount,
  onChangeRolloutCount,
  firstSeed,
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
  // Dense terminal-only percentiles are requested separately from the timeline fan. This keeps the
  // fan payload small while giving the terminal distribution enough points to render smoothly.
  const [terminalResultsById, setTerminalResultsById] = useState(() => new Map());
  const [terminalResultKeysById, setTerminalResultKeysById] = useState(() => new Map());
  const [errorsById, setErrorsById] = useState(() => new Map());
  const [terminalErrorsById, setTerminalErrorsById] = useState(() => new Map());
  const [portfolio, setPortfolio] = useState(null);
  const [portfolioError, setPortfolioError] = useState(null);
  const [selectedPercentile, setSelectedPercentile] = useState(null);
  const [selectedSeedFromDistribution, setSelectedSeedFromDistribution] = useState(null);
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
  const selectMetricValue = (value) => {
    setSelectedMetricValue(value);
    setSelectedPercentile(null);
    setSelectedSeedFromDistribution(null);
    setRolloutError(null);
    eventSelection.clear();
  };

  const requestEntries = useMemo(
    () =>
      chartScenarios.map((entry) => ({
        id: entry.id,
        label: entry.label,
        request: productMetricFanRequest(entry.input, bootstrap, selectedMetric, {
          rolloutCount,
          firstSeed,
          model,
          horizonMonths,
        }),
      })),
    [chartScenarios, bootstrap, selectedMetric, rolloutCount, firstSeed, model, horizonMonths]
  );
  const terminalRequestEntries = useMemo(
    () =>
      chartScenarios.map((entry) => ({
        id: entry.id,
        label: entry.label,
        request: productTerminalDistributionRequest(entry.input, bootstrap, selectedMetric, {
          rolloutCount,
          firstSeed,
          model,
          horizonMonths,
        }),
      })),
    [chartScenarios, bootstrap, selectedMetric, rolloutCount, firstSeed, model, horizonMonths]
  );
  const terminalRequestKeyById = useMemo(
    () => new Map(terminalRequestEntries.map(({ id, request }) => [id, JSON.stringify(request)])),
    [terminalRequestEntries]
  );
  const activeRequest = requestEntries.find((entry) => entry.id === activeId)?.request ?? requestEntries[0].request;
  const activeRawResult = resultsById.get(activeId) ?? null;
  const activeResult = activeRawResult?.metric === selectedMetric.value ? activeRawResult : null;
  const activeRawTerminalResult = terminalResultsById.get(activeId) ?? null;
  const activeTerminalRequestKey = terminalRequestKeyById.get(activeId) ?? null;
  const activeTerminalResult =
    activeRawTerminalResult?.metric === selectedMetric.value &&
    terminalResultKeysById.get(activeId) === activeTerminalRequestKey
      ? activeRawTerminalResult
      : null;
  const terminalResultsForDisplay = useMemo(() => {
    const next = new Map();
    for (const [id, result] of terminalResultsById) {
      if (
        result?.metric === selectedMetric.value &&
        terminalResultKeysById.get(id) === terminalRequestKeyById.get(id)
      ) {
        next.set(id, result);
      }
    }
    return next;
  }, [terminalResultsById, terminalResultKeysById, terminalRequestKeyById, selectedMetric.value]);
  const activeTerminalDisplayResult = activeTerminalResult ?? activeResult;
  const activeTerminalSelectionResult = terminalResultsForDisplay.get(activeId) ?? null;
  const runError = errorsById.get(activeId) ?? null;
  const terminalError = terminalErrorsById.get(activeId) ?? null;

  // One overlay series per scenario; Base is series 0 (blue), variants follow. Color is by position
  // so it stays put as the active selection moves. The active scenario also drives the histogram,
  // selected-rollout overlay, and events.
  const fanSeries = useMemo(
    () =>
      chartScenarios.map((entry, index) => ({
        id: entry.id,
        label: entry.label,
        color: scenarioColor(index),
        rows:
          resultsById.get(entry.id)?.metric === selectedMetric.value ? metricFanRows(resultsById.get(entry.id)) : [],
        isActive: entry.id === activeId,
      })),
    [chartScenarios, resultsById, activeId, selectedMetric.value]
  );

  const scenarioCacheKey = useMemo(() => JSON.stringify(activeRequest.scenario), [activeRequest.scenario]);
  const selectedTerminalSample = useMemo(
    () =>
      selectedPercentile == null
        ? null
        : terminalSampleAtPercentile(activeTerminalSelectionResult, selectedMetric, selectedPercentile),
    [activeTerminalSelectionResult, selectedMetric, selectedPercentile]
  );
  const selectedRolloutSeed = selectedTerminalSample?.seed ?? selectedSeedFromDistribution;
  const selectedDetailKey = selectedRolloutSeed == null ? null : `${scenarioCacheKey}|seed:${selectedRolloutSeed}`;
  const selectedDetail = selectedDetailKey ? rolloutDetails.get(selectedDetailKey) : null;
  const selectedSeed = selectedRolloutSeed ?? selectedDetail?.rollout?.seed ?? null;
  const selectedSummary = useMemo(
    () =>
      selectedDetail?.rollout
        ? {
            seed: Number(selectedDetail.rollout.seed),
            failed: Boolean(selectedDetail.rollout.failed),
            terminalMetrics: selectedDetail.rollout.terminalMetrics,
          }
        : null,
    [selectedDetail]
  );
  const selectedRows = useMemo(
    () => selectedRolloutMetricRows(selectedDetail, selectedMetric),
    [selectedDetail, selectedMetric]
  );
  const selectedEvents = useMemo(() => selectedRolloutEvents(selectedDetail), [selectedDetail]);
  const failedCount = activeResult?.failedCount ?? null;
  const terminalP50 = terminalPercentileValue(activeTerminalDisplayResult, 50);
  const selectedRolloutLoading =
    selectedPercentile != null && activeTerminalDisplayResult != null && !selectedDetail && !rolloutError;

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
  // Selecting in the distribution chart carries both coordinates: the line picks the variant
  // (making it active), and the X picks the percentile. The active terminal-distribution response
  // maps that percentile to a seed; the full rollout detail is fetched by seed.
  const onSelectPercentile = (variantId, percentile, seed = null) => {
    selectEntry(variantId);
    setSelectedPercentile(percentile);
    setSelectedSeedFromDistribution(seed == null ? null : Number(seed));
    setRolloutError(null);
  };
  const clearSelectedRollout = () => {
    setSelectedPercentile(null);
    setSelectedSeedFromDistribution(null);
    setRolloutError(null);
    eventSelection.clear();
  };
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
    const params = new URLSearchParams(scenarioSetToSearch(base, variants));
    if (currencyDisplay !== "compact") params.set("fmt", "exact");
    // The shell-owned shared params live outside the scenario set, as do the budget tab's planning
    // params (`bhide`/`bset`). Carry whichever are currently set across so rewriting the product
    // `?scenarios=` state doesn't drop them when switching away from and back to another tab.
    const currentParams = new URLSearchParams(window.location.search);
    for (const key of ["n", "x", "h", "scale", "fmt", "bhide", "bset"]) {
      const value = currentParams.get(key);
      if (value != null) params.set(key, value);
    }
    const search = params.toString();
    const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
    if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
      window.history.replaceState(null, "", newUrl);
    }
  }, [base, variants, currencyDisplay]);

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
        toastFetchError("product-portfolio", "Couldn't load portfolio", error);
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
    setTerminalResultsById(prune);
    setTerminalResultKeysById(prune);
    setErrorsById(prune);
    setTerminalErrorsById(prune);
  }, [chartScenarios]);

  useEffect(() => {
    const controller = new AbortController();
    // Fan out one request per scenario over the shared seed set. Results land in place (no clear)
    // so the comparison fans stay put while the active scenario is being edited.
    const handle = setTimeout(() => {
      for (const { id, label, request } of requestEntries) {
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
            toastFetchError(`product-fan-${id}`, `Projection failed: ${label}`, error);
          });
      }
    }, 120);
    return () => {
      clearTimeout(handle);
      controller.abort();
    };
  }, [requestEntries]);

  useEffect(() => {
    const controller = new AbortController();
    const handle = setTimeout(() => {
      for (const { id, label, request } of terminalRequestEntries) {
        const requestKey = JSON.stringify(request);
        fetchProductTerminalDistribution(request, { signal: controller.signal })
          .then((payload) => {
            setTerminalResultsById((previous) => new Map(previous).set(id, payload));
            setTerminalResultKeysById((previous) => new Map(previous).set(id, requestKey));
            setTerminalErrorsById((previous) => {
              if (!previous.has(id)) return previous;
              const next = new Map(previous);
              next.delete(id);
              return next;
            });
          })
          .catch((error) => {
            if (error?.name === "AbortError") return;
            setTerminalErrorsById((previous) => new Map(previous).set(id, error?.message || String(error)));
            toastFetchError(`product-terminal-${id}`, `Terminal distribution failed: ${label}`, error);
          });
      }
    }, 120);
    return () => {
      clearTimeout(handle);
      controller.abort();
    };
  }, [terminalRequestEntries]);

  useEffect(() => {
    eventSelection.clear();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDetailKey]);

  useEffect(() => {
    if (selectedPercentile == null || selectedRolloutSeed == null || selectedDetailKey == null) return;
    if (rolloutDetails.has(selectedDetailKey)) return;
    const controller = new AbortController();
    setRolloutError(null);
    fetchProductRollout(
      {
        scenario: activeRequest.scenario,
        seed: selectedRolloutSeed,
      },
      { signal: controller.signal }
    )
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
        toastFetchError("product-rollout", "Rollout detail failed", error);
      });
    return () => controller.abort();
  }, [activeRequest.scenario, rolloutDetails, selectedDetailKey, selectedPercentile, selectedRolloutSeed]);

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
          model={model}
          onChangeModel={onChangeModel}
          models={bootstrap.models}
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
          onSetBaseField={setBaseField}
          onPatchVariant={patchVariantOverrides}
          onRevertKeys={revertVariantKeys}
          onAddVariant={addVariant}
          onSelect={selectEntry}
          onDeleteVariant={deleteVariant}
          onRename={renameEntry}
          onResetBase={resetBase}
        />

        {runError && <div className="augur-note-danger p-4 text-sm">Product projection failed: {runError}</div>}
        {terminalError && (
          <div className="augur-note-danger p-4 text-sm">Terminal distribution failed: {terminalError}</div>
        )}

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
                {fmtNumber(failedCount)} / {fmtNumber(activeRequest.rolloutCount)}
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
            onSelectMetric={selectMetricValue}
            metricScale={metricScale}
            horizonMonths={horizonMonths}
            onChangeHorizonMonths={onChangeHorizonMonths}
            maxHorizonMonths={bootstrap.maxHorizonMonths}
            selectedSeed={selectedSeed}
            selectedPercentile={selectedPercentile}
            onSelectPercentile={onSelectPercentile}
            onClearSelection={clearSelectedRollout}
            selectedRolloutLoading={selectedRolloutLoading}
            fanSeries={fanSeries}
            percentiles={activeRequest.percentiles}
            scenarios={chartScenarios}
            terminalResultsById={terminalResultsForDisplay}
            activeId={activeId}
            activeTerminalResult={activeTerminalDisplayResult}
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
