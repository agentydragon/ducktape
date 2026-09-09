// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { scenarioSetToSearch } from "./input_helpers";
import { ProductProjectionWorkspace } from "./product";

const client = vi.hoisted(() => ({ portfolio: vi.fn(), projection: vi.fn(), rollout: vi.fn(), toast: vi.fn() }));
vi.mock("./client", () => ({
  fetchProductPortfolio: client.portfolio,
  fetchProductProjectionSummary: client.projection,
  fetchProductRollout: client.rollout,
}));
vi.mock("./lib/toast", () => ({ toastFetchError: client.toast }));
// Keep product request/selection state real; these render-boundary probes avoid chart geometry.
vi.mock("@mantine/core", () => ({ NativeSelect: () => null, SegmentedControl: () => null }));
vi.mock("./header", () => ({
  AugurHeader: () => null,
  SharedControls: () => null,
  AugurTabBar: () => null,
  DeploymentCommitSummary: () => null,
}));
vi.mock("./metric_table", () => ({ TerminalMetricTable: () => null, TerminalScenarioComparison: () => null }));
vi.mock("./scenario_tabs", () => ({ ScenarioBadge: () => null }));
vi.mock("./events_panel", () => ({ SelectedRolloutEventsPanel: () => null, EventKindLegend: () => null }));
vi.mock("./skeleton", () => ({ RolloutResultsSkeleton: () => null, StatCardsSkeleton: () => null }));
vi.mock("./scenario_editor", () => ({
  ScenarioEditor: ({ onSetBaseField }) => (
    <button onClick={() => onSetBaseField("monthlySpend", 2000)}>Edit base</button>
  ),
}));
vi.mock("./terminal_distribution", () => ({
  TerminalDistributionChart: ({ scenarios, onSelectPercentile, onClear }) => (
    <div>
      {scenarios.map((scenario) => (
        <span key={scenario.id}>
          <button onClick={() => onSelectPercentile(scenario.id, 0)}>{scenario.label} low</button>
          <button onClick={() => onSelectPercentile(scenario.id, 100)}>{scenario.label} high</button>
        </span>
      ))}
      <button onClick={onClear}>Clear detail</button>
    </div>
  ),
}));
vi.mock("./fan_chart", () => ({
  MetricFanChart: ({ series, selectedRows }) => (
    <div>
      <output data-testid="fans">{JSON.stringify(series.map(({ label, rows }) => [label, rows.length]))}</output>
      <output data-testid="detail">{selectedRows.map((row) => row.currencyQuanta).join(",")}</output>
    </div>
  ),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function deferred<T>() {
  let resolve: (value: T) => void;
  let reject: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function projection(request) {
  const seed = Number(request.scenario.monthlySpend) / 100;
  const common = {
    modelId: "example-model",
    currencyCode: "USD",
    currencyQuantum: "0.01",
    metric: request.metric,
    failedCount: 0,
    terminalMetricPercentiles: { percentile: [0, 50, 100], valueQuanta: ["100000", "150000", "200000"] },
  };
  return {
    metricFan: { ...common, monthlyMetricFan: { monthIndex: [0], percentile: [50], valueQuanta: ["150000"] } },
    terminalDistribution: {
      ...common,
      terminalMetricSamples: { seed: [seed, seed + 1], valueQuanta: ["100000", "200000"], failed: [false, false] },
    },
  };
}

function detail(seed: number, value: string) {
  return {
    currencyCode: "USD",
    currencyQuantum: "0.01",
    rollout: { seed, failed: false, monthlyMetrics: { monthIndex: [0], netWorthQuanta: [value] }, events: [] },
  };
}

const requests: Array<ReturnType<typeof deferred<ReturnType<typeof detail>>> & { seed: number; signal: AbortSignal }> =
  [];
const roots: Array<ReturnType<typeof createRoot>> = [];

async function mount() {
  const search = scenarioSetToSearch({ label: "Base", input: { monthlySpend: 1000 } }, [
    { label: "Variant", overrides: { monthlySpend: 1500 } },
  ]);
  window.history.replaceState(null, "", `/?${search}`);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  await act(async () => {
    root.render(
      <ProductProjectionWorkspace
        bootstrap={{ locations: [], models: ["example-model"], maxRolloutSamples: 100, maxHorizonMonths: 120 }}
        deployment={null}
        tab="product"
        onSelectTab={vi.fn()}
        rolloutCount={2}
        onChangeRolloutCount={vi.fn()}
        firstSeed={1}
        model="example-model"
        onChangeModel={vi.fn()}
        horizonMonths={24}
        onChangeHorizonMonths={vi.fn()}
        metricScale="linear"
        onChangeMetricScale={vi.fn()}
        currencyDisplay="compact"
        onChangeCurrencyDisplay={vi.fn()}
        settingsOpen={false}
        onChangeSettingsOpen={vi.fn()}
      />
    );
  });
  await act(async () => vi.advanceTimersByTimeAsync(120));
}

async function click(label: string) {
  const button = [...document.querySelectorAll("button")].find((entry) => entry.textContent === label);
  if (!button) throw new Error(`Missing ${label} button`);
  await act(async () => button.click());
}

async function resolveDetail(index: number, value: string) {
  await act(async () => {
    requests[index].resolve(detail(requests[index].seed, value));
    await requests[index].promise;
  });
}

function displayedDetail() {
  return document.querySelector('[data-testid="detail"]')?.textContent;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  requests.length = 0;
  client.portfolio.mockResolvedValue({ holdings: [] });
  client.projection.mockImplementation((request) => Promise.resolve(projection(request)));
  client.rollout.mockImplementation((request, { signal }) => {
    const response = { ...deferred<ReturnType<typeof detail>>(), seed: request.seed, signal };
    requests.push(response);
    return response.promise;
  });
});

afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
  document.body.replaceChildren();
  vi.useRealTimers();
});

it("re-fetches revisited seeds, drops prior detail, and preserves every displayed fan", async () => {
  await mount();
  const fans = document.querySelector('[data-testid="fans"]')?.textContent;
  expect(fans).toContain("Base");
  expect(fans).toContain("Variant");
  await click("Base low");
  await resolveDetail(0, "101");
  expect(displayedDetail()).toBe("101");
  await click("Base high");
  expect(displayedDetail()).toBe("");
  await resolveDetail(1, "202");
  expect(displayedDetail()).toBe("202");
  await click("Base low");
  expect(requests.map((request) => request.seed)).toEqual([10, 11, 10]);
  expect(displayedDetail()).toBe("");
  await resolveDetail(2, "303");
  expect(displayedDetail()).toBe("303");
  expect(document.querySelector('[data-testid="fans"]')?.textContent).toBe(fans);
  expect(client.projection).toHaveBeenCalledTimes(2);
});

it("ignores late success and failure responses after switching scenario or clearing selection", async () => {
  await mount();
  await click("Base low");
  await click("Variant high");
  expect(requests.map((request) => request.seed)).toEqual([10, 16]);
  expect(requests[0].signal.aborted).toBe(true);
  await resolveDetail(1, "222");
  await resolveDetail(0, "111");
  expect(displayedDetail()).toBe("222");
  await click("Base high");
  await click("Clear detail");
  expect(requests[2].signal.aborted).toBe(true);
  await act(async () => {
    requests[2].reject(new Error("obsolete detail failed"));
    await requests[2].promise.catch(() => undefined);
  });
  expect(displayedDetail()).toBe("");
  expect(client.toast).not.toHaveBeenCalled();
  await click("Variant high");
  expect(requests.map((request) => request.seed)).toEqual([10, 16, 11, 16]);
});

it("waits for a changed scenario's current percentile-to-seed mapping while retaining fans", async () => {
  await mount();
  await click("Base low");
  await resolveDetail(0, "111");
  const fans = document.querySelector('[data-testid="fans"]')?.textContent;
  await click("Edit base");
  expect(displayedDetail()).toBe("");
  expect(requests).toHaveLength(1);
  expect(document.querySelector('[data-testid="fans"]')?.textContent).toBe(fans);
  await act(async () => vi.advanceTimersByTimeAsync(120));
  expect(requests.map((request) => request.seed)).toEqual([10, 20]);
  expect(client.rollout.mock.calls[1][0].scenario.monthlySpend).toBe("2000");
  await resolveDetail(1, "222");
  expect(displayedDetail()).toBe("222");
});

it("scopes an error to the selected detail and clears it when selecting another seed", async () => {
  await mount();
  await click("Base low");
  await act(async () => {
    requests[0].reject(new Error("current detail failed"));
    await requests[0].promise.catch(() => undefined);
  });
  expect(document.body.textContent).toContain("current detail failed");
  expect(client.toast).toHaveBeenCalledTimes(1);
  await click("Base high");
  expect(document.body.textContent).not.toContain("current detail failed");
  await resolveDetail(1, "222");
  expect(displayedDetail()).toBe("222");
});
