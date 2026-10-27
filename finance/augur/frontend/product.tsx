import React, { useEffect, useMemo, useState } from "react";
import { NativeSelect, SegmentedControl } from "@mantine/core";

import {
  fetchProductPortfolio,
  fetchProductProjectionSummary,
  fetchProductRollout,
  type AugurDeployment,
  type ProductPortfolioResponse,
} from "./client";
import { fmtQuanta, fmtNumber } from "./lib/format";
import { rowsFrom } from "./lib/frame";
import { toastFetchError } from "./lib/toast";
import { replaceSearchParams } from "./url_state";

import { MetricFanChart, type FanSeries } from "./fan_chart";
import { TerminalDistributionChart } from "./terminal_distribution";
import { TerminalMetricTable, TerminalScenarioComparison } from "./metric_table";
import { SelectedRolloutEventsPanel, EventKindLegend } from "./events_panel";
import { ScenarioEditor } from "./scenario_editor";
import { ScenarioBadge } from "./scenario_tabs";
import { AugurHeader, SharedControls, AugurTabBar, DeploymentCommitSummary, type AugurTab } from "./header";
import { RolloutResultsSkeleton, StatCardsSkeleton } from "./skeleton";
import { useVisibleEventKinds, useEventSelection } from "./hooks";
import {
  FAN_PERCENTILES,
  METRIC_OPTIONS,
  MAX_VARIANTS,
  TERMINAL_DISTRIBUTION_PERCENTILES,
  productInputDefaults,
  productProjectionSummaryRequest,
  scenarioSetToSearch,
  scenarioSetFromSearch,
  makeVariant,
  resolveVariant,
  defaultVariantLabel,
  scenarioColor,
} from "./input_helpers";
import {
  metricFanRows,
  terminalPercentileValue,
  terminalSampleAtPercentile,
  selectedRolloutMetricRows,
  selectedRolloutEvents,
  visibleMetricOptions,
  sellableSleeves,
} from "./data_helpers";

type ProjectionSummaryResponse = Awaited<ReturnType<typeof fetchProductProjectionSummary>>;
type ScenarioEditorProps = React.ComponentProps<typeof ScenarioEditor>;
type ProductInput = ScenarioEditorProps["base"]["input"];
type Bootstrap = ScenarioEditorProps["bootstrap"];
type MetricOption = (typeof METRIC_OPTIONS)[number];
type MetricScale = React.ComponentProps<typeof SharedControls>["metricScale"];
type CurrencyDisplay = React.ComponentProps<typeof SharedControls>["currencyDisplay"];
type SharedControlProps = React.ComponentProps<typeof SharedControls>;
type Scenario = { id: string; label: string; input: ProductInput };
type TerminalResults = ReadonlyMap<string, ProjectionSummaryResponse["terminalDistribution"]>;
type SelectedSummary = NonNullable<React.ComponentProps<typeof TerminalMetricTable>["selectedSummary"]>;
type EventSelection = ReturnType<typeof useEventSelection>;
type EventVisibility = ReturnType<typeof useVisibleEventKinds>;
type ProductWorkspaceProps = {
  bootstrap: Bootstrap;
  deployment: AugurDeployment | null;
  tab: AugurTab;
  onSelectTab: (tab: AugurTab) => void;
  rolloutCount: SharedControlProps["rolloutCount"];
  onChangeRolloutCount: NonNullable<SharedControlProps["onChangeRolloutCount"]>;
  firstSeed: number;
  model: SharedControlProps["model"];
  onChangeModel: NonNullable<SharedControlProps["onChangeModel"]>;
  horizonMonths: number | null;
  onChangeHorizonMonths: (value: number | null) => void;
  metricScale: MetricScale;
  onChangeMetricScale: NonNullable<SharedControlProps["onChangeMetricScale"]>;
  currencyDisplay: CurrencyDisplay;
  onChangeCurrencyDisplay: NonNullable<SharedControlProps["onChangeCurrencyDisplay"]>;
  settingsOpen: boolean;
  onChangeSettingsOpen: SharedControlProps["onChangeSettingsOpen"];
};
type ProjectionSelection =
  { kind: "percentile"; value: number } | { kind: "stopped"; seed: number; scenarioKey: string } | null;
type ProjectionSnapshot = {
  requestKey: string;
  metricFan: ProjectionSummaryResponse["metricFan"];
  terminalDistribution: ProjectionSummaryResponse["terminalDistribution"];
};
type ProjectionResource =
  | { status: "pending"; requestKey: string; stale: ProjectionSnapshot | null; error: string | null }
  | { status: "ready"; snapshot: ProjectionSnapshot }
  | { status: "failed"; requestKey: string; stale: ProjectionSnapshot | null; error: string };

function errorMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error && error.message) {
    return String(error.message);
  }
  return String(error);
}

function isAbortError(error: unknown): boolean {
  return typeof error === "object" && error !== null && "name" in error && error.name === "AbortError";
}

function metricOption(value: string): MetricOption {
  const option = METRIC_OPTIONS.find((candidate) => candidate.value === value);
  if (!option) throw new Error(`unknown product metric: ${value}`);
  return option;
}

function projectionSnapshot(resource: ProjectionResource | undefined): ProjectionSnapshot | null {
  if (!resource) return null;
  switch (resource.status) {
    case "ready":
      return resource.snapshot;
    case "pending":
    case "failed":
      return resource.stale;
  }
}

function projectionError(resource: ProjectionResource | undefined): string | null {
  return resource && resource.status !== "ready" ? resource.error : null;
}

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
}: {
  visibleMetrics: readonly MetricOption[];
  selectedMetric: MetricOption;
  onSelectMetric: (value: string) => void;
  metricScale: MetricScale;
  horizonMonths: number | null;
  onChangeHorizonMonths: (value: number | null) => void;
  maxHorizonMonths: number;
  selectedSeed: number | null;
  selectedPercentile: number | null;
  onSelectPercentile: (scenarioId: string, percentile: number) => void;
  onClearSelection: () => void;
  selectedRolloutLoading: boolean;
  fanSeries: readonly FanSeries[];
  percentiles: readonly number[];
  scenarios: readonly Scenario[];
  terminalResultsById: TerminalResults;
  activeId: string;
  activeTerminalResult:
    ProjectionSummaryResponse["metricFan"] | ProjectionSummaryResponse["terminalDistribution"] | null;
  selectedRows: ReturnType<typeof selectedRolloutMetricRows>;
  selectedEvents: ReturnType<typeof selectedRolloutEvents>;
  selectedSummary: SelectedSummary | null;
  visibleEventKinds: EventVisibility;
  eventSelection: EventSelection;
  rolloutError: string | null;
}) {
  const [chartMode, setChartMode] = useState<"fan" | "candles">("fan");
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
    <section
      className="augur-panel overflow-hidden"
      aria-label="Cash projection workspace"
      data-product-results-ready=""
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="flex items-center gap-3">
          <NativeSelect
            aria-label="Metric to plot"
            value={selectedMetric.value}
            data={visibleMetrics.map((metric) => ({ value: metric.value, label: metric.label }))}
            classNames={{ input: "augur-tabular min-w-[12rem]" }}
            onChange={(event) => onSelectMetric(event.target.value)}
          />
          {multipleScenarios && activeScenario && (
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
            onChange={(value) => setChartMode(value === "candles" ? "candles" : "fan")}
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
}: ProductWorkspaceProps) {
  const [scenarioSet, setScenarioSet] = useState(() => scenarioSetFromSearch(window.location.search, bootstrap));
  const [selectedMetricValue, setSelectedMetricValue] = useState<MetricOption["value"]>("net_worth");
  // Every scenario owns one resource that carries the latest request state plus its last successful
  // response. The backend returns the fan and terminal distribution from one simulation.
  const [projectionResourcesById, setProjectionResourcesById] = useState<Map<string, ProjectionResource>>(
    () => new Map()
  );
  const [portfolio, setPortfolio] = useState<ProductPortfolioResponse | null>(null);
  const [portfolioError, setPortfolioError] = useState<string | null>(null);
  const [selection, setSelection] = useState<ProjectionSelection>(null);
  const selectedPercentile = selection?.kind === "percentile" ? selection.value : null;
  const [selectedRollout, setSelectedRollout] = useState<{
    key: string;
    detail: Awaited<ReturnType<typeof fetchProductRollout>> | null;
    error: string | null;
  } | null>(null);
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
    const visibleValues = new Set<MetricOption["value"]>();
    for (const entry of chartScenarios)
      for (const metric of visibleMetricOptions(entry.input)) visibleValues.add(metric.value);
    return METRIC_OPTIONS.filter((metric) => visibleValues.has(metric.value));
  }, [chartScenarios]);
  const selectedMetric =
    visibleMetrics.find((metric) => metric.value === selectedMetricValue) ?? visibleMetrics[0] ?? METRIC_OPTIONS[0];
  const selectMetricValue = (value: string) => {
    setSelectedMetricValue(metricOption(value).value);
    setSelection(null);
    eventSelection.clear();
  };

  // `null` until the portfolio fetch lands — distinct from "loaded, and nothing is sellable".
  // An unedited target allocation is seeded from these rows, so a request built before the
  // portfolio arrives would ask for a no-target scenario (never auto-sells, so ruin) and get a
  // fan the user never asked for. The fetch effects below wait for it rather than showing that.
  const sellable = useMemo(() => (portfolio ? sellableSleeves(portfolio) : null), [portfolio]);
  const projectionRequestEntries = useMemo(
    () =>
      chartScenarios.map((entry) => {
        const request = productProjectionSummaryRequest(
          entry.input,
          bootstrap,
          selectedMetric,
          {
            rolloutCount,
            firstSeed,
            model,
            horizonMonths,
            sellable: sellable ?? [],
          },
          FAN_PERCENTILES,
          TERMINAL_DISTRIBUTION_PERCENTILES
        );
        return { id: entry.id, label: entry.label, request, requestKey: JSON.stringify(request) };
      }),
    [chartScenarios, bootstrap, selectedMetric, rolloutCount, firstSeed, model, horizonMonths, sellable]
  );
  const activeRequestEntry =
    projectionRequestEntries.find((entry) => entry.id === activeId) ?? projectionRequestEntries[0];
  const activeRequest = activeRequestEntry.request;
  const activeProjection = projectionSnapshot(projectionResourcesById.get(activeId));
  const activeResult = activeProjection?.metricFan.metric === selectedMetric.value ? activeProjection.metricFan : null;
  const activeTerminalResult =
    activeProjection?.terminalDistribution.metric === selectedMetric.value &&
    activeProjection.requestKey === activeRequestEntry.requestKey
      ? activeProjection.terminalDistribution
      : null;
  const terminalResultsForDisplay = useMemo(() => {
    const next = new Map<string, ProjectionSummaryResponse["terminalDistribution"]>();
    for (const entry of projectionRequestEntries) {
      const snapshot = projectionSnapshot(projectionResourcesById.get(entry.id));
      if (snapshot?.terminalDistribution.metric === selectedMetric.value && snapshot.requestKey === entry.requestKey) {
        next.set(entry.id, snapshot.terminalDistribution);
      }
    }
    return next;
  }, [projectionRequestEntries, projectionResourcesById, selectedMetric.value]);
  const activeTerminalDisplayResult = activeTerminalResult ?? activeResult;
  const activeTerminalSelectionResult = terminalResultsForDisplay.get(activeId) ?? null;
  const projectionFailure = projectionError(projectionResourcesById.get(activeId));

  // One overlay series per scenario; Base is series 0 (blue), variants follow. Color is by position
  // so it stays put as the active selection moves. The active scenario also drives the histogram,
  // selected-rollout overlay, and events.
  const fanSeries = useMemo(
    () =>
      chartScenarios.map((entry, index) => {
        const metricFan = projectionSnapshot(projectionResourcesById.get(entry.id))?.metricFan;
        return {
          id: entry.id,
          label: entry.label,
          color: scenarioColor(index),
          rows: metricFan?.metric === selectedMetric.value ? metricFanRows(metricFan) : [],
          isActive: entry.id === activeId,
        };
      }),
    [chartScenarios, projectionResourcesById, activeId, selectedMetric.value]
  );

  const scenarioKey = useMemo(() => JSON.stringify(activeRequest.scenario), [activeRequest.scenario]);
  const selectedTerminalSample = useMemo(
    () =>
      selectedPercentile == null
        ? null
        : terminalSampleAtPercentile(activeTerminalSelectionResult, selectedMetric, selectedPercentile),
    [activeTerminalSelectionResult, selectedMetric, selectedPercentile]
  );
  const stoppedSeeds = rowsFrom(activeTerminalSelectionResult?.terminalMetricSamples)
    .filter((row) => row.failed)
    .map((row) => Number(row.seed));
  const selectedRolloutSeed =
    selection?.kind === "stopped"
      ? selection.scenarioKey === scenarioKey && stoppedSeeds.includes(selection.seed)
        ? selection.seed
        : null
      : (selectedTerminalSample?.seed ?? null);
  const selectedDetailKey = selectedRolloutSeed == null ? null : `${scenarioKey}|seed:${selectedRolloutSeed}`;
  const selectedDetail = selectedRollout?.key === selectedDetailKey ? selectedRollout.detail : null;
  const rolloutError = selectedRollout?.key === selectedDetailKey ? selectedRollout.error : null;
  const selectedSeed = selectedRolloutSeed;
  const selectedSummary = useMemo(
    () =>
      selectedDetail?.rollout
        ? {
            seed: Number(selectedDetail.rollout.seed),
            failed: Boolean(selectedDetail.rollout.failed),
            endingMetrics: selectedDetail.rollout.endingMetrics,
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
    selectedRolloutSeed != null && activeTerminalDisplayResult != null && !selectedDetail && !rolloutError;

  // -- Base + variant operations. Base edits propagate to every variant that doesn't override the
  // touched knob (variants resolve as `{ ...base, ...overrides }`); variant edits write to that
  // variant's `overrides` only. ----------------------------------------------------------------
  const updateBaseInput = (patch: Partial<ProductInput>) =>
    setScenarioSet((previous) => ({
      ...previous,
      base: { ...previous.base, input: { ...previous.base.input, ...patch } },
    }));
  const setBaseField: ScenarioEditorProps["onSetBaseField"] = (key, value) => updateBaseInput({ [key]: value });
  const resetBase = () =>
    setScenarioSet((previous) => ({ ...previous, base: { ...previous.base, input: productInputDefaults(bootstrap) } }));
  const selectEntry = (id: string) => setScenarioSet((previous) => ({ ...previous, activeId: id }));
  // Selecting in the distribution chart carries both coordinates: the line picks the variant
  // (making it active), and the X picks the percentile. The active terminal-distribution response
  // maps that percentile to a seed; the full rollout detail is fetched by seed.
  const onSelectPercentile = (variantId: string, percentile: number) => {
    selectEntry(variantId);
    setSelection({ kind: "percentile", value: percentile });
  };
  const clearSelectedRollout = () => {
    setSelection(null);
    eventSelection.clear();
  };
  const renameEntry = (id: string, label: string) =>
    setScenarioSet((previous) =>
      id === "base"
        ? { ...previous, base: { ...previous.base, label } }
        : { ...previous, variants: previous.variants.map((v) => (v.id === id ? { ...v, label } : v)) }
    );
  // Merge a partial-override patch into one variant's `overrides` (scalar cells + the variant
  // housing panel). Overridden keys win over the inherited base values.
  const patchVariantOverrides = (id: string, patch: Partial<ProductInput>) =>
    setScenarioSet((previous) => ({
      ...previous,
      variants: previous.variants.map((v) => (v.id === id ? { ...v, overrides: { ...v.overrides, ...patch } } : v)),
    }));
  // Drop override keys so the variant re-inherits the base value(s): a scalar cell's "revert" or the
  // whole housing cluster's "revert to base".
  const revertVariantKeys = (id: string, keys: (keyof ProductInput)[]) =>
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
  const deleteVariant = (id: string) =>
    setScenarioSet((previous) => ({
      ...previous,
      variants: previous.variants.filter((v) => v.id !== id),
      activeId: previous.activeId === id ? "base" : previous.activeId,
    }));

  useEffect(() => {
    // The active selection is ephemeral UI state, not persisted: the codec always decodes Base as
    // active, so reloading a shared link lands on Base.
    const params = new URLSearchParams(scenarioSetToSearch(base, variants));
    replaceSearchParams({ scenarios: params.get("scenarios") });
  }, [base, variants]);

  useEffect(() => {
    const controller = new AbortController();
    fetchProductPortfolio({ signal: controller.signal })
      .then((payload) => {
        setPortfolio(payload);
        setPortfolioError(null);
      })
      .catch((error) => {
        if (isAbortError(error)) return;
        setPortfolio(null);
        setPortfolioError(errorMessage(error));
        toastFetchError("product-portfolio", "Couldn't load portfolio", error);
      });
    return () => controller.abort();
  }, []);

  // Drop cached projection resources for scenarios that no longer exist (deleted from the set).
  useEffect(() => {
    const ids = new Set(chartScenarios.map((entry) => entry.id));
    setProjectionResourcesById((previous) => {
      const next = new Map([...previous].filter(([id]) => ids.has(id)));
      return next.size === previous.size ? previous : next;
    });
  }, [chartScenarios]);

  useEffect(() => {
    if (sellable == null) return;
    const controller = new AbortController();
    // Fan out one combined request per scenario. The backend performs the metric-fan and terminal
    // reductions after one shared simulation, while the two public legacy endpoints remain intact.
    const handle = setTimeout(() => {
      for (const { id, label, request, requestKey } of projectionRequestEntries) {
        setProjectionResourcesById((previous) => {
          const previousResource = previous.get(id);
          return new Map(previous).set(id, {
            status: "pending",
            requestKey,
            stale: projectionSnapshot(previousResource),
            error: projectionError(previousResource),
          });
        });
        fetchProductProjectionSummary(request, { signal: controller.signal })
          .then((payload) => {
            if (controller.signal.aborted) return;
            setProjectionResourcesById((previous) => {
              const current = previous.get(id);
              if (current?.status !== "pending" || current.requestKey !== requestKey) return previous;
              return new Map(previous).set(id, {
                status: "ready",
                snapshot: {
                  requestKey,
                  metricFan: payload.metricFan,
                  terminalDistribution: payload.terminalDistribution,
                },
              });
            });
          })
          .catch((error) => {
            if (controller.signal.aborted || isAbortError(error)) return;
            const message = errorMessage(error);
            setProjectionResourcesById((previous) => {
              const current = previous.get(id);
              if (current?.status !== "pending" || current.requestKey !== requestKey) return previous;
              return new Map(previous).set(id, {
                status: "failed",
                requestKey,
                stale: current.stale,
                error: message,
              });
            });
            toastFetchError(`product-projection-${id}`, `Projection failed: ${label}`, error);
          });
      }
    }, 120);
    return () => {
      clearTimeout(handle);
      controller.abort();
    };
  }, [projectionRequestEntries, sellable]);

  useEffect(() => {
    eventSelection.clear();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDetailKey]);

  useEffect(() => {
    setSelectedRollout(null);
    if (selectedRolloutSeed == null || selectedDetailKey == null) return;
    const controller = new AbortController();
    fetchProductRollout(
      {
        scenario: activeRequest.scenario,
        seed: selectedRolloutSeed,
      },
      { signal: controller.signal }
    )
      .then((payload) => {
        if (controller.signal.aborted) return;
        setSelectedRollout({ key: selectedDetailKey, detail: payload, error: null });
      })
      .catch((error) => {
        if (controller.signal.aborted || isAbortError(error)) return;
        setSelectedRollout({ key: selectedDetailKey, detail: null, error: errorMessage(error) });
        toastFetchError("product-rollout", "Rollout detail failed", error);
      });
    return () => controller.abort();
  }, [activeRequest.scenario, selectedDetailKey, selectedPercentile, selectedRolloutSeed]);

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

        {projectionFailure && (
          <>
            <div className="augur-note-danger p-4 text-sm">Product projection failed: {projectionFailure}</div>
            <div className="augur-note-danger p-4 text-sm">Terminal distribution failed: {projectionFailure}</div>
          </>
        )}

        {activeResult ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="augur-card p-4">
              <div className="augur-eyebrow">
                Median {activeResult.basis === "observed_through_stop" ? "recorded" : "terminal"}{" "}
                {selectedMetric.label.toLowerCase()}
              </div>
              <div className="text-xs augur-muted">
                {activeResult.basis === "observed_through_stop" ? "Through stop/completion" : "Completed paths only"} (
                {activeResult.observationCount} observations)
              </div>
              <div className="mt-2 text-2xl font-semibold augur-tabular">
                {fmtQuanta(terminalP50, {
                  currencyCode: activeTerminalDisplayResult?.currencyCode,
                  currencyQuantum: activeTerminalDisplayResult?.currencyQuantum,
                })}
              </div>
            </div>
            <div className="augur-card p-4">
              <div className="augur-eyebrow">Failed rollouts</div>
              <div className="mt-2 text-2xl font-semibold augur-tabular">
                {fmtNumber(failedCount)} / {fmtNumber(activeRequest.rolloutCount)}
              </div>
              {stoppedSeeds.length > 0 && (
                <NativeSelect
                  aria-label="Inspect stopped rollout"
                  value={selection?.kind === "stopped" ? String(selectedRolloutSeed ?? "") : ""}
                  data={[
                    { value: "", label: "Inspect a stopped path" },
                    ...stoppedSeeds.map((seed) => ({ value: String(seed), label: `Seed ${seed}` })),
                  ]}
                  onChange={(event) =>
                    setSelection(
                      event.target.value === ""
                        ? null
                        : { kind: "stopped", seed: Number(event.target.value), scenarioKey }
                    )
                  }
                />
              )}
              {selectedSummary?.failed && selectedDetail != null && (
                <div className="mt-2 text-sm" data-product-stop-book="">
                  At stop in event month {selectedSummary.endingMetrics.failedMonthIndex} (snapshot{" "}
                  {selectedSummary.endingMetrics.snapshotIndex}):{" "}
                  {fmtQuanta(selectedSummary.endingMetrics[selectedMetric.chartValue], {
                    currencyCode: selectedDetail.currencyCode,
                    currencyQuantum: selectedDetail.currencyQuantum,
                  })}
                  .{" "}
                  {activeResult.basis === "observed_through_stop"
                    ? "Recorded through stopping, not projected future shortfall."
                    : "Not horizon-terminal wealth."}
                </div>
              )}
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
            percentiles={activeRequest.fanPercentiles}
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
