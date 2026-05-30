import React, { useEffect, useMemo, useState } from "react";
import { MantineProvider, NativeSelect, SegmentedControl } from "@mantine/core";

import {
  fetchAugurBootstrap,
  fetchAugurDeployment,
  fetchProductMetricFan,
  fetchProductPortfolio,
  fetchProductRollout,
} from "./client.js";
import { fmtNumber } from "./lib/format.js";
import { fmtMetricValue } from "./lib/chart.js";

import { MetricFanChart } from "./fan_chart.jsx";
import { TerminalDistributionHistogram } from "./histogram.jsx";
import { TerminalMetricTable } from "./metric_table.jsx";
import { SelectedRolloutEventsPanel, EventKindLegend } from "./events_panel.jsx";
import { ProductScenarioForm } from "./forms.jsx";
import { CalibrationWorkspace } from "./calibration.jsx";
import { AugurHeader } from "./header.jsx";
import { RolloutResultsSkeleton, StatCardsSkeleton, ProductProjectionLoading } from "./skeleton.jsx";
import { CurrencyDisplayProvider, useCurrencyDisplay, useVisibleEventKinds, useEventSelection } from "./hooks.js";
import {
  METRIC_OPTIONS,
  productInputDefaults,
  productInputToSearch,
  productInputFromSearch,
  productMetricFanRequest,
  rolloutCountFromSearch,
  rolloutCountDefault,
  clampRolloutCount,
} from "./input_helpers.js";
import {
  metricFanRows,
  terminalPercentileValue,
  selectedRolloutMetricRows,
  selectedRolloutEvents,
  visibleMetricOptions,
} from "./data_helpers.js";

const METRIC_SCALE_OPTIONS = [
  { value: "linear", label: "Linear" },
  { value: "log", label: "Log" },
];

const CURRENCY_DISPLAY_OPTIONS = [
  { value: "exact", label: "Exact" },
  { value: "compact", label: "Compact" },
];

// Top-level views. "product" is the default; the active tab is mirrored to the URL `?tab=`
// (omitted for the default), following the same replaceState pattern as the product `?s=` state.
const TABS = [
  { value: "product", label: "Product" },
  { value: "calibration", label: "Calibration" },
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
  onSelectMetricScale,
  rolloutSummaries,
  selectedSeed,
  onSelectSeed,
  selectedRolloutLoading,
  fanRows,
  percentiles,
  selectedRows,
  selectedEvents,
  selectedSummary,
  visibleEventKinds,
  eventSelection,
  rolloutError,
}) {
  const { display: currencyDisplay, setDisplay: onSelectCurrencyDisplay } = useCurrencyDisplay();
  return (
    <section className="augur-panel overflow-hidden" aria-label="Cash projection workspace">
      <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <NativeSelect
            aria-label="Metric to plot"
            value={selectedMetric.value}
            data={visibleMetrics.map((metric) => ({ value: metric.value, label: metric.label }))}
            classNames={{ input: "augur-tabular min-w-[12rem]" }}
            onChange={(event) => onSelectMetric(event.target.value)}
          />
          <div className="flex flex-wrap items-center gap-3">
            <div data-product-currency-display-control>
              <SegmentedControl
                aria-label="Currency display"
                size="xs"
                value={currencyDisplay}
                data={CURRENCY_DISPLAY_OPTIONS}
                onChange={onSelectCurrencyDisplay}
              />
            </div>
            <div data-product-chart-scale-control>
              <SegmentedControl
                aria-label="Chart scale"
                size="xs"
                value={metricScale}
                data={METRIC_SCALE_OPTIONS}
                onChange={onSelectMetricScale}
              />
            </div>
          </div>
        </div>
      </div>
      <TerminalDistributionHistogram
        summaries={rolloutSummaries}
        metric={selectedMetric}
        metricScale={metricScale}
        selectedSeed={selectedSeed}
        loadingSeed={selectedRolloutLoading ? selectedSeed : null}
        onSelect={onSelectSeed}
      />
      {fanRows.length > 0 ? (
        <MetricFanChart
          rows={fanRows}
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
      ) : (
        <div className="flex min-h-[22rem] items-center justify-center text-sm augur-muted">Running...</div>
      )}
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

function viewPrefsFromSearch(searchString) {
  const params = new URLSearchParams(searchString);
  return {
    metricScale: params.get("scale") === "log" ? "log" : "linear",
    currencyDisplay: params.get("fmt") === "exact" ? "exact" : "compact",
  };
}

function ProductProjectionWorkspace({ bootstrap, deployment, tab, onSelectTab, rolloutCount, onChangeRolloutCount }) {
  const initialViewPrefs = viewPrefsFromSearch(window.location.search);
  const [input, setInput] = useState(() => productInputFromSearch(window.location.search, bootstrap));
  const [selectedMetricValue, setSelectedMetricValue] = useState("net_worth_usd");
  const [metricScale, setMetricScale] = useState(initialViewPrefs.metricScale);
  const [currencyDisplay, setCurrencyDisplay] = useState(initialViewPrefs.currencyDisplay);
  const [result, setResult] = useState(null);
  const [runError, setRunError] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [portfolioError, setPortfolioError] = useState(null);
  const [selectedSeed, setSelectedSeed] = useState(null);
  const [rolloutDetails, setRolloutDetails] = useState(() => new Map());
  const [rolloutError, setRolloutError] = useState(null);
  const eventSelection = useEventSelection();
  const visibleEventKinds = useVisibleEventKinds();
  const visibleMetrics = useMemo(() => visibleMetricOptions(input), [input]);
  const selectedMetric =
    visibleMetrics.find((metric) => metric.value === selectedMetricValue) ?? visibleMetrics[0] ?? METRIC_OPTIONS[0];
  const request = useMemo(
    () => productMetricFanRequest(input, bootstrap, selectedMetric, rolloutCount),
    [input, bootstrap, selectedMetric, rolloutCount]
  );
  const scenarioCacheKey = useMemo(() => JSON.stringify(request.scenario), [request.scenario]);
  const fanRows = useMemo(() => metricFanRows(result), [result]);
  const rolloutSummaries = result?.rolloutSummaries ?? [];
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
  const failedCount = result?.failedCount ?? null;
  const terminalP50 = terminalPercentileValue(result, 50);
  const updateInput = (patch) => setInput((previous) => ({ ...previous, ...patch }));
  const selectedRolloutLoading = selectedSeed != null && result != null && !selectedDetail && !rolloutError;

  useEffect(() => {
    const params = new URLSearchParams(productInputToSearch(input, bootstrap));
    if (metricScale !== "linear") params.set("scale", "log");
    if (currencyDisplay !== "compact") params.set("fmt", "exact");
    // The shared rollout count (`?n=`) is owned by the shell, not the product input. Carry the
    // current value across so rewriting the product state doesn't drop it.
    const sharedRolloutCount = new URLSearchParams(window.location.search).get("n");
    if (sharedRolloutCount != null) params.set("n", sharedRolloutCount);
    const search = params.toString();
    const newUrl = `${window.location.pathname}${search ? "?" + search : ""}${window.location.hash}`;
    if (newUrl !== window.location.pathname + window.location.search + window.location.hash) {
      window.history.replaceState(null, "", newUrl);
    }
  }, [input, bootstrap, metricScale, currencyDisplay]);

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

  useEffect(() => {
    const controller = new AbortController();
    setResult(null);
    const handle = setTimeout(() => {
      fetchProductMetricFan(request, { signal: controller.signal })
        .then((payload) => {
          setResult(payload);
          setRunError(null);
        })
        .catch((error) => {
          if (error?.name === "AbortError") return;
          setResult(null);
          setRunError(error?.message || String(error));
        });
    }, 120);
    return () => {
      clearTimeout(handle);
      controller.abort();
    };
  }, [request]);

  useEffect(() => {
    if (selectedSeed == null || !result?.rolloutSummaries) return;
    if (!result.rolloutSummaries.some((summary) => Number(summary.seed) === selectedSeed)) {
      setSelectedSeed(null);
    }
  }, [result, selectedSeed]);

  useEffect(() => {
    eventSelection.clear();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDetailKey]);

  useEffect(() => {
    if (selectedSeed == null || result == null || selectedDetailKey == null) return;
    if (rolloutDetails.has(selectedDetailKey)) return;
    const controller = new AbortController();
    setRolloutError(null);
    fetchProductRollout({ scenario: request.scenario, seed: selectedSeed }, { signal: controller.signal })
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
  }, [request.scenario, result, rolloutDetails, selectedDetailKey, selectedSeed]);

  const currencyDisplayContext = useMemo(
    () => ({ display: currencyDisplay, setDisplay: setCurrencyDisplay }),
    [currencyDisplay]
  );

  return (
    <CurrencyDisplayProvider value={currencyDisplayContext}>
      <div
        data-augur-surface="product"
        className="min-h-screen bg-slate-100 text-slate-800 dark:bg-slate-950 dark:text-slate-100"
      >
        <AugurHeader
          nav={<AugurTabBar tab={tab} onSelectTab={onSelectTab} />}
          rightSlot={
            <>
              <DeploymentCommitSummary deployment={deployment} />
              <span className="whitespace-nowrap">{fmtNumber(request.rolloutSeeds.length)} rollouts</span>
            </>
          }
        />

        <main className="px-4 py-6 sm:px-6 lg:px-8">
          <section className="grid min-w-0 gap-5 min-[864px]:grid-cols-[28rem_minmax(0,1fr)]">
            <ProductScenarioForm
              input={input}
              bootstrap={bootstrap}
              portfolio={portfolio}
              portfolioError={portfolioError}
              onChange={updateInput}
              onReset={() => setInput(productInputDefaults(bootstrap))}
              rolloutCount={rolloutCount}
              onChangeRolloutCount={onChangeRolloutCount}
            />

            <div className="min-w-0 space-y-5">
              {runError && <div className="augur-note-danger p-4 text-sm">Product projection failed: {runError}</div>}

              {result ? (
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
                      {fmtNumber(failedCount)} / {fmtNumber(request.rolloutSeeds.length)}
                    </div>
                  </div>
                </div>
              ) : (
                <StatCardsSkeleton />
              )}

              {result ? (
                <RolloutResultsPanel
                  visibleMetrics={visibleMetrics}
                  selectedMetric={selectedMetric}
                  onSelectMetric={setSelectedMetricValue}
                  metricScale={metricScale}
                  onSelectMetricScale={setMetricScale}
                  rolloutSummaries={rolloutSummaries}
                  selectedSeed={selectedSeed}
                  onSelectSeed={setSelectedSeed}
                  selectedRolloutLoading={selectedRolloutLoading}
                  fanRows={fanRows}
                  percentiles={request.percentiles}
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
            </div>
          </section>
        </main>
      </div>
    </CurrencyDisplayProvider>
  );
}

function CalibrationAppSurface({ bootstrap, deployment, tab, onSelectTab, rolloutCount, onChangeRolloutCount }) {
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
          onChangeRolloutCount={onChangeRolloutCount}
        />
      </main>
    </div>
  );
}

// Mounted once bootstrap has loaded so the shared rollout-count default (which depends on
// `bootstrap.maxRolloutSamples`) is available at state-init time. Owns the cross-tab state —
// the active tab and the shared rollout count — and hands both to whichever workspace is active.
function LoadedAppShell({ bootstrap, deployment }) {
  const [tab, setTab] = useState(() => tabFromSearch(window.location.search));
  const [rolloutCount, setRolloutCount] = useState(() => rolloutCountFromSearch(window.location.search, bootstrap));

  const onSelectTab = (next) => {
    setTab(next);
    writeTabToSearch(next);
  };

  const onChangeRolloutCount = (value) => {
    const next = value == null ? value : clampRolloutCount(value, bootstrap);
    setRolloutCount(next);
    writeRolloutCountToSearch(next, bootstrap);
  };

  const sharedProps = { bootstrap, deployment, tab, onSelectTab, rolloutCount, onChangeRolloutCount };
  if (tab === "calibration") {
    return <CalibrationAppSurface {...sharedProps} />;
  }
  return <ProductProjectionWorkspace {...sharedProps} />;
}

function ProductProjectionAppShell() {
  const [bootstrap, setBootstrap] = useState(null);
  const [bootstrapError, setBootstrapError] = useState(null);
  const [deployment, setDeployment] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchAugurBootstrap({ signal: controller.signal })
      .then((payload) => {
        setBootstrap(payload);
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
