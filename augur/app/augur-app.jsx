import React, { useEffect, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  CloseButton,
  Collapse,
  ColorInput,
  Group,
  MantineProvider,
  NativeSelect,
  NumberInput,
  Radio,
  Stack,
  Tabs,
  Text,
  TextInput,
} from "@mantine/core";

import { rowsFromCamelColumnar } from "./lib/columnar.js";
import {
  SCENARIO_COLORS,
  createDefaultScenarioSetInput,
  createScenarioInput,
  normalizeScenarioSetInput,
  patchScenarioInputSection,
  privateEquityCurrentUnitPriceUsd,
  privateEquityValueUsdForUnits,
  scenarioSetInputFromUrlSearch,
  scenarioSetInputToRequest,
  searchWithScenarioSetInput,
  uniqueScenarioId,
} from "./lib/scenario_set_state.js";
import { fetchAugurBootstrap, runScenarioSet } from "./augur_client.js";

const FINANCING_OPTIONS = [
  { id: "fixed_30", label: "30-year fixed" },
  { id: "fixed_15", label: "15-year fixed" },
  { id: "custom", label: "Custom override" },
  { id: "cash", label: "Cash" },
];

const PRIVATE_EQUITY_SALE_POLICY_OPTIONS = [
  {
    id: "none",
    label: "Do not sell",
    description: "Tender opportunities do not trigger private-stock sales.",
  },
  {
    id: "liquid_net_worth_floor",
    label: "Sell at liquid-worth floor",
    description: "When cash plus SP500 is below the floor and a tender exists, sell the configured amount into SP500.",
  },
];

const CHECKING_FLOOR_POLICY_ID = "checking_floor_sp500";
const PRIVATE_EQUITY_LIQUID_FLOOR_POLICY_ID = "liquid_net_worth_floor";
const CHECKING_FLOOR_METRICS = new Set([
  "checkingFloorShortfallUsd",
  "finalCheckingFloorShortfallUsd",
  "totalGenericSp500SaleUsd",
  "genericSp500SaleUsd",
]);
const CONTROL_GRID_CLASS = "grid min-w-0 grid-cols-[repeat(auto-fit,minmax(min(100%,11rem),1fr))] gap-3";
const FAN_CHART_TICK_FRACTIONS = [0, 0.25, 0.5, 0.75, 1];
const RESULT_VIEW_MODES = new Set(["distribution", "trajectory"]);
const RESULT_PANEL_KINDS = new Set(["distribution", "trajectory", "accounting_detail"]);
const ROLLOUT_STATUS_ACTIVE = "active";
const ROLLOUT_STATUS_CASH_NEGATIVE = "cash_negative";

function viewModeFromPathname(pathname) {
  const segment = String(pathname ?? "")
    .replace(/\/+$/u, "")
    .split("/")
    .filter(Boolean)
    .at(-1);
  return RESULT_VIEW_MODES.has(segment) ? segment : "distribution";
}

function pathForViewMode(viewMode) {
  return viewMode === "trajectory" ? "/trajectory" : "/distribution";
}

function rolloutIndexFromSearch(search) {
  const value = Number(new URLSearchParams(search).get("rollout"));
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function searchScenarioId(search) {
  return new URLSearchParams(search).get("scenario");
}

function searchWithAppState(search, input, selectedScenarioId, selectedRolloutIndex) {
  const nextSearch = searchWithScenarioSetInput(search, input);
  const params = new URLSearchParams(nextSearch.startsWith("?") ? nextSearch.slice(1) : nextSearch);
  if (selectedScenarioId) {
    params.set("scenario", selectedScenarioId);
  } else {
    params.delete("scenario");
  }
  params.set("rollout", String(Math.max(0, Math.floor(Number(selectedRolloutIndex) || 0))));
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

function scenarioIdentity(scenario) {
  return scenario?.identity ?? {};
}

function scenarioPropertyAndLocation(scenario) {
  return scenario?.propertyAndLocation ?? {};
}

function scenarioActorsAndOwnership(scenario) {
  return scenario?.actorsAndOwnership ?? {};
}

function scenarioTimeline(scenario) {
  return scenario?.timeline ?? {};
}

function scenarioFinancing(scenario) {
  return scenario?.financing ?? {};
}

function scenarioOccupancyAndRental(scenario) {
  return scenario?.occupancyAndRental ?? {};
}

function scenarioPropertyAssumptions(scenario) {
  return scenario?.propertyAssumptions ?? {};
}

function scenarioTaxAccounting(scenario) {
  return scenario?.taxAccounting ?? {};
}

function scenarioInitialBalanceSheet(scenario) {
  return scenario?.initialBalanceSheet ?? {};
}

function scenarioPolicies(scenario) {
  return scenario?.policies ?? {};
}

function scenarioIdOf(scenario) {
  return scenarioIdentity(scenario).scenarioId;
}

function scenarioSectionPatch(scenarioSetInput, scenarioId, section, patch) {
  return {
    ...scenarioSetInput,
    scenarios: scenarioSetInput.scenarios.map((scenario) =>
      scenarioIdOf(scenario) === scenarioId ? patchScenarioInputSection(scenario, section, patch) : scenario
    ),
  };
}

function scenarioUsesCheckingFloorPolicy(scenario) {
  return scenarioPolicies(scenario).liquidReservePolicy === CHECKING_FLOOR_POLICY_ID;
}

function scenarioSetUsesCheckingFloorPolicy(scenarioSetInput) {
  return (scenarioSetInput?.scenarios ?? []).some(scenarioUsesCheckingFloorPolicy);
}

function optionLabel(options, id) {
  return options.find((option) => option.id === id)?.label ?? id ?? "n/a";
}

function assertResultViewKind(view, kind) {
  if (view?.kind !== kind) {
    throw new Error(`Expected ${kind} Augur result view, got ${view?.kind ?? "<missing>"}`);
  }
}

function rolloutIndexesFromRows(rows) {
  return [...new Set(rows.map((row) => Number(row.rolloutIndex)).filter(Number.isFinite))].sort(
    (left, right) => left - right
  );
}

function selectedRolloutFromRows(rows, requestedRolloutIndex) {
  const rolloutIndexes = rolloutIndexesFromRows(rows);
  const rolloutIndex = rolloutIndexes.includes(requestedRolloutIndex)
    ? requestedRolloutIndex
    : (rolloutIndexes[0] ?? 0);
  return {
    rolloutIndexes,
    rolloutIndex,
    rows: rows.filter((row) => Number(row.rolloutIndex) === rolloutIndex),
  };
}

function sumRows(rows, column) {
  return rows.reduce((total, row) => total + (Number(row[column]) || 0), 0);
}

function rowsHaveAny(rows, columns) {
  return rows.some((row) => columns.some((column) => Boolean(row[column])));
}

function annualRowsFromRows(rows, { includeInitial = true, limit = 8 } = {}) {
  return rows
    .filter((row) => (includeInitial ? row.monthIndex === 0 || row.monthIndex % 12 === 0 : row.monthIndex % 12 === 0))
    .slice(0, limit);
}

function distributionResultView(scenarioResult) {
  const terminal = terminalRows(scenarioResult);
  const hasMetricFanColumns = Object.keys(scenarioResult?.metricFanColumns ?? {}).length > 0;
  return {
    kind: "distribution",
    hasMetricFanColumns,
    hasTerminalRows: terminal.length > 0,
    metricFanRows: (metricName) => metricFanRows(scenarioResult, metricName),
    metricFanTerminal: (metricName) => metricFanTerminal(scenarioResult, metricName),
    terminalRows: () => terminal,
    terminalP50: (column) => terminalP50(scenarioResult, column),
  };
}

function selectedRolloutResultView(scenarioResult, selectedRolloutIndex, kind) {
  const monthlyRows = rowsFromTable(scenarioResult?.monthlyColumns);
  const selected = selectedRolloutFromRows(monthlyRows, selectedRolloutIndex);
  return {
    kind,
    hasRows: selected.rows.length > 0,
    rolloutIndexes: selected.rolloutIndexes,
    rolloutIndex: selected.rolloutIndex,
    selectedRows: selected.rows,
    annualRows: (options) => annualRowsFromRows(selected.rows, options),
    terminalRow: () => selected.rows.at(-1) ?? null,
    sum: (column) => sumRows(selected.rows, column),
    hasAny: (columns) => rowsHaveAny(selected.rows, columns),
    rowsWhere: (predicate) => selected.rows.filter(predicate),
  };
}

function trajectoryResultView(scenarioResult, selectedRolloutIndex) {
  return selectedRolloutResultView(scenarioResult, selectedRolloutIndex, "trajectory");
}

function accountingDetailResultView(scenarioResult, selectedRolloutIndex) {
  return selectedRolloutResultView(scenarioResult, selectedRolloutIndex, "accounting_detail");
}

function fmtUsd(value) {
  if (!Number.isFinite(value)) return "n/a";
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function fmtPct(value) {
  if (!Number.isFinite(value)) return "n/a";
  return `${(value * 100).toFixed(1)}%`;
}

function fmtNumber(value) {
  if (!Number.isFinite(value)) return "n/a";
  return value.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function fmtInteger(value) {
  if (!Number.isFinite(value)) return "n/a";
  return value.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function metricIsCurrency(metricName) {
  return metricName?.endsWith("Usd") || metricName?.includes("Value") || metricName?.includes("CashFlow");
}

function fmtMetricValue(metricName, value) {
  if (metricName?.endsWith("Pct") || metricName === "partnerOwnershipPct") {
    return fmtPct(value);
  }
  if (metricIsCurrency(metricName)) {
    return fmtUsd(value);
  }
  return fmtNumber(value);
}

function fmtAxisMetricValue(metricName, value) {
  if (!metricIsCurrency(metricName)) {
    return fmtMetricValue(metricName, value);
  }
  if (!Number.isFinite(value)) return "n/a";
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: Math.abs(value) >= 1_000_000 ? 2 : 1,
  });
}

function niceCurrencyTickStep(rawStep) {
  if (!Number.isFinite(rawStep) || rawStep <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const normalized = rawStep / magnitude;
  const niceNormalized = [1, 2, 2.5, 5, 10].find((candidate) => normalized <= candidate) ?? 10;
  return Math.max(1, niceNormalized * magnitude);
}

function currencyFanChartAxis(values, targetTickCount = 5) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const step = niceCurrencyTickStep(span === 0 ? Math.max(Math.abs(max), 1) / 2 : span / (targetTickCount - 1));
  let axisMin = Math.floor(min / step) * step;
  let axisMax = Math.ceil(max / step) * step;
  if (axisMin === axisMax) {
    axisMin -= step * 2;
    axisMax += step * 2;
  }
  const ticks = [];
  for (let value = axisMax, guard = 0; value >= axisMin - step / 2 && guard < 12; value -= step, guard += 1) {
    ticks.push(Math.round(value / step) * step);
  }
  return { min: axisMin, max: axisMax, range: axisMax - axisMin, ticks };
}

function fanChartAxis(metricName, values) {
  if (values.length === 0) {
    return {
      min: 0,
      max: 1,
      range: 1,
      ticks: FAN_CHART_TICK_FRACTIONS.map((tick) => 1 - tick),
    };
  }
  if (metricIsCurrency(metricName)) {
    return currencyFanChartAxis(values);
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max === min ? 1 : max - min;
  return {
    min,
    max: min + range,
    range,
    ticks: FAN_CHART_TICK_FRACTIONS.map((tick) => min + range * (1 - tick)),
  };
}

function labelFromCamel(value) {
  if (!value) return "";
  const withSpaces = value
    .replace(/Usd$/u, "")
    .replace(/Pct$/u, " pct")
    .replace(/([a-z0-9])([A-Z])/gu, "$1 $2")
    .replace(/\bSp500\b/gu, "SP500");
  return withSpaces.charAt(0).toUpperCase() + withSpaces.slice(1);
}

function rowsFromTable(table) {
  return table ? rowsFromCamelColumnar(table) : [];
}

function lastRow(table) {
  const rows = rowsFromTable(table);
  return rows.length > 0 ? rows[rows.length - 1] : null;
}

function median(values) {
  const finiteValues = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (finiteValues.length === 0) return NaN;
  const middle = Math.floor(finiteValues.length / 2);
  if (finiteValues.length % 2 === 1) return finiteValues[middle];
  return (finiteValues[middle - 1] + finiteValues[middle]) / 2;
}

function p50Column(rows, column) {
  return median(rows.map((row) => Number(row[column])));
}

function propertyLocation(property, locationsById) {
  if (!property) return null;
  return locationsById.get(property.locationId) ?? null;
}

function propertyLabel(property, locationsById) {
  if (!property) return "Unknown property";
  const location = propertyLocation(property, locationsById);
  return `${property.address} · ${location?.label ?? property.locationId ?? "Unknown location"}`;
}

function primaryConcentratedHolding(bootstrap) {
  return bootstrap?.financeSnapshot?.concentratedHoldings?.[0] ?? null;
}

function concentratedHoldingValueUsd(holding) {
  const explicitValue = Number(holding?.valueUsd);
  if (Number.isFinite(explicitValue)) return explicitValue;
  const units = Number(holding?.units);
  const fmv = Number(holding?.fmvUsdPerUnit);
  return Number.isFinite(units) && Number.isFinite(fmv) ? units * fmv : NaN;
}

function scenarioResultById(result, scenarioId) {
  return result?.scenarioResults?.find((item) => item.scenarioId === scenarioId) ?? null;
}

function metricFanRows(scenarioResult, metricName) {
  return rowsFromTable(scenarioResult?.metricFanColumns?.[metricName]);
}

function terminalRows(scenarioResult) {
  return rowsFromTable(scenarioResult?.terminalColumns);
}

function terminalP50(scenarioResult, column) {
  return p50Column(terminalRows(scenarioResult), column);
}

function rolloutStatuses(scenarioResult) {
  return Array.isArray(scenarioResult?.rolloutStatuses) ? scenarioResult.rolloutStatuses : [];
}

function rolloutStatusSummary(scenarioResult) {
  const statuses = rolloutStatuses(scenarioResult);
  return {
    total: statuses.length,
    active: statuses.filter((status) => status?.status === ROLLOUT_STATUS_ACTIVE).length,
    cashNegative: statuses.filter((status) => status?.status === ROLLOUT_STATUS_CASH_NEGATIVE).length,
  };
}

function fmtRolloutStatusShare(count, total) {
  if (!Number.isFinite(total) || total <= 0) return "unknown";
  return `${fmtInteger(count)} / ${fmtInteger(total)} (${fmtPct(count / total)})`;
}

function RolloutHealthCell({ statusSummary }) {
  return (
    <td className="min-w-[10rem] whitespace-nowrap">
      <div className="space-y-1 text-right">
        <div>
          <span className="augur-muted">Active</span> {fmtRolloutStatusShare(statusSummary.active, statusSummary.total)}
        </div>
        <div>
          <span className="augur-muted">Cash negative</span>{" "}
          {fmtRolloutStatusShare(statusSummary.cashNegative, statusSummary.total)}
        </div>
      </div>
    </td>
  );
}

function rolloutStatusForIndex(scenarioResult, rolloutIndex) {
  const target = Number(rolloutIndex);
  if (!Number.isInteger(target) || target < 0) return null;
  return rolloutStatuses(scenarioResult).find((status) => Number(status?.rolloutIndex) === target) ?? null;
}

function selectedRolloutStatusText(status) {
  if (!status) return "status unknown";
  const minCash = fmtUsd(Number(status.minCashUsd));
  if (status.status === ROLLOUT_STATUS_ACTIVE) {
    return `active; minimum cash ${minCash}`;
  }
  if (status.status === ROLLOUT_STATUS_CASH_NEGATIVE) {
    const firstNegativeMonth = Number(status.firstNegativeCashMonthIndex);
    const monthText = Number.isFinite(firstNegativeMonth)
      ? `first negative cash month ${fmtInteger(firstNegativeMonth)}`
      : "first negative cash month unknown";
    return `cash went negative; ${monthText}; minimum cash ${minCash}`;
  }
  return `status ${String(status.status ?? "unknown")}`;
}

function metricFanTerminal(scenarioResult, metricName) {
  return lastRow(scenarioResult?.metricFanColumns?.[metricName]);
}

function metricOptionsFromResult(result, scenarioSetInput) {
  const metricNames = new Set();
  for (const scenarioResult of result?.scenarioResults ?? []) {
    for (const metricName of Object.keys(scenarioResult.metricFanColumns ?? {})) {
      metricNames.add(metricName);
    }
  }
  if (!scenarioSetUsesCheckingFloorPolicy(scenarioSetInput)) {
    metricNames.delete("checkingFloorShortfallUsd");
  }
  const preferred = [
    "netWorthUsd",
    "liquidNetWorthUsd",
    "cashUsd",
    "genericSp500ValueUsd",
    "propertyValueUsd",
    "homeEquityUsd",
    "mortgageBalanceUsd",
    "rentalIncomeUsd",
    "netPropertyCashFlowUsd",
    "propertySaleNetProceedsUsd",
    "netPropertySaleCashFlowUsd",
    "privateEquityValueUsd",
    "privateEquitySaleOpportunityValueUsd",
    "partnerHomeEquityClaimUsd",
    "partnerOwnershipPct",
    "checkingFloorShortfallUsd",
  ];
  return [...metricNames].sort((left, right) => {
    const leftIndex = preferred.indexOf(left);
    const rightIndex = preferred.indexOf(right);
    if (leftIndex !== -1 || rightIndex !== -1) {
      return (leftIndex === -1 ? 999 : leftIndex) - (rightIndex === -1 ? 999 : rightIndex);
    }
    return left.localeCompare(right);
  });
}

function scenarioFanRows(result, scenarioId, metricName) {
  const scenarioResult = scenarioResultById(result, scenarioId);
  return metricFanRows(scenarioResult, metricName);
}

function OptionButtons({ label, options, value, onChange }) {
  return (
    <Radio.Group value={value} onChange={onChange} label={label} classNames={{ label: "augur-field-label mb-2 block" }}>
      <Stack gap="xs">
        {options.map((option) => {
          return (
            <Radio.Card key={option.id} value={option.id} radius="md" p="md" withBorder>
              <Group wrap="nowrap" align="flex-start" gap="sm">
                <Radio.Indicator mt={2} />
                <Stack gap={3} className="min-w-0">
                  <Text size="sm" fw={650} lh={1.2}>
                    {option.label}
                  </Text>
                  {option.description && (
                    <Text size="xs" c="dimmed" lh={1.25}>
                      {option.description}
                    </Text>
                  )}
                </Stack>
              </Group>
            </Radio.Card>
          );
        })}
      </Stack>
    </Radio.Group>
  );
}

function ControlGrid({ children, className = "" }) {
  return <div className={`${CONTROL_GRID_CLASS} ${className}`}>{children}</div>;
}

function numberFieldSectionWidth(section) {
  if (!section) return undefined;
  return Math.max(34, String(section).length * 8 + 24);
}

function NumberField({ label, value, onChange, min = 0, step = 1000, prefix = null, suffix = null }) {
  const formattedPrefix = prefix ? String(prefix).trim() : undefined;
  const formattedSuffix = suffix ? String(suffix).trim() : undefined;
  return (
    <NumberInput
      label={label}
      aria-label={label}
      min={min}
      step={step}
      value={value ?? ""}
      hideControls
      leftSection={formattedPrefix ? <Text className="augur-number-section">{formattedPrefix}</Text> : undefined}
      leftSectionPointerEvents="none"
      leftSectionWidth={numberFieldSectionWidth(formattedPrefix)}
      rightSection={formattedSuffix ? <Text className="augur-number-section">{formattedSuffix}</Text> : undefined}
      rightSectionPointerEvents="none"
      rightSectionWidth={numberFieldSectionWidth(formattedSuffix)}
      thousandSeparator=","
      classNames={{ label: "augur-field-label mb-2 block", input: "augur-tabular" }}
      onChange={(nextValue) => {
        const number = typeof nextValue === "number" ? nextValue : Number(nextValue);
        onChange(Number.isFinite(number) ? number : null);
      }}
    />
  );
}

function MoneyField(props) {
  return <NumberField prefix="$" {...props} />;
}

function ReadOnlyMetricField({ label, value, detail = null }) {
  return (
    <div className="min-w-0 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-700 dark:bg-slate-900/70">
      <div className="mb-1 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
        {label}
      </div>
      <div className="mono truncate text-sm font-semibold augur-strong">{value}</div>
      {detail && <div className="mt-1 truncate text-xs augur-muted">{detail}</div>}
    </div>
  );
}

function SelectField({ label, value, onChange, options }) {
  return (
    <NativeSelect
      label={label}
      aria-label={label}
      value={value}
      data={options.map((option) => ({ value: option.id, label: option.label }))}
      classNames={{ label: "augur-field-label mb-2 block" }}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

function ControlSection({ title, children }) {
  return (
    <div className="border-t border-slate-200 pt-4 dark:border-slate-700">
      <div className="mb-3 augur-eyebrow">{title}</div>
      {children}
    </div>
  );
}

function assertResultPanelKind(kind) {
  if (!RESULT_PANEL_KINDS.has(kind)) {
    throw new Error(`Unknown Augur result panel kind: ${kind}`);
  }
}

function ResultPanelHeader({ title, subtitle = null, actions = null }) {
  return (
    <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <div className="augur-eyebrow">{title}</div>
          </div>
          {subtitle && <div className="mt-1 text-sm augur-muted">{subtitle}</div>}
        </div>
        {actions}
      </div>
    </div>
  );
}

function ResultPanel({ kind, title, subtitle = null, actions = null, children, className = "" }) {
  assertResultPanelKind(kind);
  return (
    <section className={`augur-card overflow-hidden ${className}`} data-result-panel-kind={kind}>
      <ResultPanelHeader title={title} subtitle={subtitle} actions={actions} />
      {children}
    </section>
  );
}

function DisclosurePanel({ title, subtitle = null, summary = null, children, defaultOpen = false, marker = {} }) {
  const [opened, setOpened] = useState(defaultOpen);
  return (
    <section className="augur-card overflow-hidden" {...marker}>
      <button
        type="button"
        className="flex w-full min-w-0 items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 text-left dark:border-slate-700"
        aria-expanded={opened}
        onClick={() => setOpened((previous) => !previous)}
      >
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="text-xs augur-muted">{opened ? "▼" : "▶"}</span>
            <span className="augur-eyebrow">{title}</span>
            {summary && <span className="text-xs augur-muted">{summary}</span>}
          </div>
          {subtitle && <div className="mt-1 text-sm augur-muted">{subtitle}</div>}
        </div>
      </button>
      <Collapse in={opened} transitionDuration={0}>
        {children}
      </Collapse>
    </section>
  );
}

function RunContextDisclosurePanel({ context, title, subtitle = null, summary = null, children, defaultOpen = false }) {
  return (
    <DisclosurePanel
      title={title}
      subtitle={subtitle}
      summary={summary}
      defaultOpen={defaultOpen}
      marker={{ "data-run-context-panel": context }}
    >
      {children}
    </DisclosurePanel>
  );
}

function ScenarioContextDisclosurePanel({
  context,
  title,
  subtitle = null,
  summary = null,
  children,
  defaultOpen = false,
}) {
  return (
    <DisclosurePanel
      title={title}
      subtitle={subtitle}
      summary={summary}
      defaultOpen={defaultOpen}
      marker={{ "data-scenario-context-panel": context }}
    >
      {children}
    </DisclosurePanel>
  );
}

function ScenarioValueSummary({ distribution }) {
  assertResultViewKind(distribution, "distribution");
  if (!distribution.hasMetricFanColumns) {
    return <div className="augur-note">Scenario details are waiting for central scenario-engine results.</div>;
  }
  const rows = [
    ["P50 net worth", fmtUsd(distribution.metricFanTerminal("netWorthUsd")?.p50)],
    ["P50 cash", fmtUsd(distribution.metricFanTerminal("cashUsd")?.p50)],
    ["P50 liquid net worth", fmtUsd(distribution.metricFanTerminal("liquidNetWorthUsd")?.p50)],
    ["P50 home equity", fmtUsd(distribution.metricFanTerminal("homeEquityUsd")?.p50)],
  ];
  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" data-result-panel-kind="distribution">
      {rows.map(([label, value]) => (
        <div key={label} className="augur-card px-4 py-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <div className="augur-eyebrow">{label}</div>
          </div>
          <div className="mt-1 mono text-lg font-semibold augur-strong">{value}</div>
        </div>
      ))}
    </section>
  );
}

function DetailTable({ rows }) {
  return (
    <div className="max-w-full overflow-x-auto">
      <table className="w-full table-fixed">
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <td className="label w-[42%] max-w-[12rem] align-top">{label}</td>
              <td className="break-words text-right align-top [overflow-wrap:anywhere]">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PortfolioSnapshotPanel({ bootstrap }) {
  const snapshot = bootstrap?.financeSnapshot;
  if (!snapshot) return null;
  const holding = primaryConcentratedHolding(bootstrap);
  const rows = [
    ["As of", snapshot.asOfDate ?? "n/a"],
    ["Cash", fmtUsd(Number(snapshot.cashUsd))],
    ["Wealthfront SP500", fmtUsd(Number(snapshot.wealthfrontSp500Usd))],
    ["IBKR VT", fmtUsd(Number(snapshot.ibkrVtUsd))],
    ["SP500-like total", fmtUsd(Number(snapshot.sp500ProxyPortfolioUsd))],
  ];
  if (holding) {
    rows.push([`${holding.label} units`, fmtNumber(Number(holding.units))]);
    rows.push([`${holding.label} value`, fmtUsd(concentratedHoldingValueUsd(holding))]);
  }
  return (
    <div className="max-w-2xl">
      <DetailTable rows={rows} />
    </div>
  );
}

function PropertyLocationPanel({ selection }) {
  const { property, location, scenario, scenarioResult } = selection;
  const timeline = scenarioTimeline(scenario);
  if (!property) return null;
  const localRegulation = location?.localRegulation ?? {};
  return (
    <section className="augur-card overflow-hidden" data-scenario-context-panel="property-location">
      <ResultPanelHeader title="Property and location" />
      <div className="grid min-w-0 gap-4 p-4 lg:grid-cols-[minmax(0,18rem)_minmax(0,1fr)]">
        <div className="min-w-0 overflow-hidden rounded-lg border border-slate-200 bg-slate-100 dark:border-slate-700 dark:bg-slate-800">
          {property.imageUrl ? (
            <img className="block aspect-[4/3] h-auto max-w-full w-full object-cover" src={property.imageUrl} alt="" />
          ) : (
            <div className="flex aspect-[4/3] items-center justify-center px-4 text-center text-sm augur-muted">
              No image
            </div>
          )}
        </div>
        <div className="min-w-0">
          <DetailTable
            rows={[
              ["Price", fmtUsd(property.priceUsd)],
              ["Rent estimate", fmtUsd(property.rentEstimateUsd)],
              ["Beds / baths", `${property.beds} / ${property.baths}`],
              ["Interior", `${fmtNumber(property.sqft)} sf`],
              ["Year built", fmtNumber(property.yearBuilt)],
              ["HOA", `${fmtUsd(property.hoaMonthlyUsd)} / mo`],
              ["Location property tax", fmtPct((localRegulation.propertyTaxAnnualPct ?? NaN) / 100)],
              ["Local transfer tax", fmtPct((localRegulation.localTransferTaxPct ?? NaN) / 100)],
              ["Special assessment", `${fmtUsd(localRegulation.specialAssessmentAnnualUsd ?? 0)} / yr`],
              ["Location id", scenarioResult?.summary?.locationId ?? property.locationId ?? "n/a"],
              ["Hold period", scenario ? `${fmtNumber(timeline.holdYears)} yr` : "n/a"],
            ]}
          />
        </div>
      </div>
    </section>
  );
}

function ScenarioFinancingTaxPanel({ selection }) {
  const { property, scenario } = selection;
  if (!scenario) return null;
  const financing = scenarioFinancing(scenario);
  const propertyAssumptions = scenarioPropertyAssumptions(scenario);
  const taxAccounting = scenarioTaxAccounting(scenario);
  const purchasePrice = Number(property?.priceUsd ?? property?.purchasePriceUsd);
  const downPaymentPct = Number(financing.downPaymentPct);
  const isCashPurchase = financing.financingMode === "cash";
  const downPayment =
    Number.isFinite(purchasePrice) && Number.isFinite(downPaymentPct)
      ? isCashPurchase
        ? purchasePrice
        : purchasePrice * (downPaymentPct / 100)
      : NaN;
  const loanAmount =
    Number.isFinite(purchasePrice) && Number.isFinite(downPayment) ? Math.max(0, purchasePrice - downPayment) : NaN;
  const customMortgageRows =
    financing.financingMode === "custom"
      ? [
          ["Custom mortgage rate", fmtPct(financing.customMortgageRate / 100)],
          ["Custom mortgage term", `${fmtNumber(financing.customMortgageTermYears)} yr`],
        ]
      : [];
  return (
    <ScenarioContextDisclosurePanel
      context="financing-tax"
      title="Scenario financing and tax assumptions"
      summary={`${optionLabel(FINANCING_OPTIONS, financing.financingMode)} · ${fmtPct(downPaymentPct / 100)} down`}
    >
      <DetailTable
        rows={[
          ["Financing mode", optionLabel(FINANCING_OPTIONS, financing.financingMode)],
          ["Purchase price", fmtUsd(purchasePrice)],
          ["Down payment", `${fmtUsd(downPayment)} (${fmtPct(downPaymentPct / 100)})`],
          ["Loan amount", fmtUsd(loanAmount)],
          ["Credit score", fmtInteger(financing.creditScore)],
          ...customMortgageRows,
          ["Buy closing cost", fmtPct(taxAccounting.closingCostBuyPct / 100)],
          ["Sell closing cost", fmtPct(taxAccounting.closingCostSellPct / 100)],
          ["Capital gains exclusion", fmtUsd(taxAccounting.capGainsExclusionUsd)],
          ["Marginal tax rate", fmtPct(taxAccounting.marginalTaxRate / 100)],
          ["Capital gains rate", fmtPct(taxAccounting.capGainsRate / 100)],
          ["Depreciable basis", fmtPct(propertyAssumptions.depreciableBasisPct / 100)],
        ]}
      />
    </ScenarioContextDisclosurePanel>
  );
}

function TerminalPercentileSnapshot({ distribution }) {
  assertResultViewKind(distribution, "distribution");
  if (!distribution.hasMetricFanColumns) return null;
  const rows = [
    ["Net worth", "netWorthUsd", fmtUsd],
    ["Liquid net worth", "liquidNetWorthUsd", fmtUsd],
    ["Cash", "cashUsd", fmtUsd],
    ["SP500 value", "genericSp500ValueUsd", fmtUsd],
    ["Property value", "propertyValueUsd", fmtUsd],
    ["Home equity", "homeEquityUsd", fmtUsd],
    ["Mortgage", "mortgageBalanceUsd", fmtUsd],
    ["Property sale proceeds", "propertySaleNetProceedsUsd", fmtUsd],
    ["Sale cash flow", "netPropertySaleCashFlowUsd", fmtUsd],
    ["Private equity value", "privateEquityValueUsd", fmtUsd],
    ["Partner equity", "partnerHomeEquityClaimUsd", fmtUsd],
    ["Partner ownership", "partnerOwnershipPct", fmtPct],
  ]
    .map(([label, metricName, formatter]) => [label, distribution.metricFanTerminal(metricName), formatter])
    .filter(([, row]) => row);
  return (
    <ResultPanel kind="distribution" title="Terminal rollout percentiles">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th className="text-left">Metric</th>
              <th>P05</th>
              <th>P50</th>
              <th>P95</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([label, row, formatter]) => (
              <tr key={label}>
                <td className="label">{label}</td>
                <td>{formatter(row?.p05)}</td>
                <td>{formatter(row?.p50)}</td>
                <td>{formatter(row?.p95)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </ResultPanel>
  );
}

function ScenarioPathPreview({ trajectory }) {
  assertResultViewKind(trajectory, "trajectory");
  const annualRows = trajectory.annualRows();
  if (annualRows.length === 0) return null;
  return (
    <ResultPanel kind="trajectory" title="Annual snapshot">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th>Year</th>
              <th>Net worth</th>
              <th>Cash</th>
              <th>Property value</th>
              <th>Home equity</th>
              <th>Mortgage</th>
              <th>Rent income</th>
              <th>Carry costs</th>
              <th>Sale cash flow</th>
            </tr>
          </thead>
          <tbody>
            {annualRows.map((row) => (
              <tr key={row.monthIndex}>
                <td>{(row.monthIndex / 12).toFixed(0)}</td>
                <td>{fmtUsd(row.netWorthUsd)}</td>
                <td>{fmtUsd(row.cashUsd)}</td>
                <td>{fmtUsd(row.propertyValueUsd)}</td>
                <td>{fmtUsd(row.homeEquityUsd)}</td>
                <td>{fmtUsd(row.mortgageBalanceUsd)}</td>
                <td>{fmtUsd(row.rentalIncomeUsd)}</td>
                <td>{fmtUsd(row.propertyCarryingCostUsd)}</td>
                <td>{fmtUsd(row.netPropertySaleCashFlowUsd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </ResultPanel>
  );
}

function PropertySaleTaxDistributionPanel({ distribution }) {
  assertResultViewKind(distribution, "distribution");
  if (!distribution.hasTerminalRows) return null;
  return (
    <ResultPanel kind="distribution" title="Property sale and tax">
      <DetailTable
        rows={[
          ["Purchase closing costs", fmtUsd(distribution.terminalP50("totalPurchaseClosingCostUsd"))],
          ["Final loan balance", fmtUsd(distribution.terminalP50("finalMortgageBalanceUsd"))],
          ["Terminal home value", fmtUsd(distribution.terminalP50("finalPropertyValueUsd"))],
          ["Sale gross", fmtUsd(distribution.terminalP50("totalPropertySaleGrossUsd"))],
          ["Selling costs", fmtUsd(distribution.terminalP50("totalSaleClosingCostUsd"))],
          ["Debt payoff", fmtUsd(distribution.terminalP50("totalPropertySaleDebtPayoffUsd"))],
          ["Sale tax", fmtUsd(distribution.terminalP50("totalPropertySaleTaxUsd"))],
          ["Realized gain", fmtUsd(distribution.terminalP50("totalRealizedPropertyGainUsd"))],
          ["Taxable gain", fmtUsd(distribution.terminalP50("totalTaxablePropertyGainUsd"))],
          ["Depreciation recapture", fmtUsd(distribution.terminalP50("totalDepreciationRecaptureUsd"))],
          ["Cumulative depreciation", fmtUsd(distribution.terminalP50("finalCumulativePropertyDepreciationUsd"))],
          ["Net sale proceeds", fmtUsd(distribution.terminalP50("totalPropertySaleNetProceedsUsd"))],
          ["Net sale cash flow", fmtUsd(distribution.terminalP50("totalNetPropertySaleCashFlowUsd"))],
        ]}
      />
    </ResultPanel>
  );
}

function PartnerOwnershipPanel({ trajectory, bootstrap }) {
  assertResultViewKind(trajectory, "trajectory");
  const partner = bootstrap?.agents?.find((a) => a.role === "equity_building_occupant");
  const partnerLabel = partner?.label ?? "Partner";
  if (!trajectory.hasRows) return null;
  const annualRows = trajectory.annualRows();
  const terminalRow = trajectory.terminalRow();
  if (!trajectory.hasAny(["partnerPresent", "partnerContributionUsd", "partnerHomeEquityClaimUsd"])) return null;
  const firstPathContribution = trajectory.sum("partnerContributionUsd");
  return (
    <ResultPanel kind="trajectory" title={`${partnerLabel} contribution and equity`}>
      <div className="grid gap-px border-b border-slate-200 bg-slate-200 dark:border-slate-700 dark:bg-slate-700 sm:grid-cols-4">
        {[
          ["Path contribution", fmtUsd(firstPathContribution)],
          ["Contribution used", fmtUsd(trajectory.sum("partnerContributionUsedUsd"))],
          ["Equity claim", fmtUsd(terminalRow?.partnerHomeEquityClaimUsd)],
          ["Final ownership", fmtPct(terminalRow?.partnerOwnershipPct)],
        ].map(([label, value]) => (
          <div key={label} className="bg-white px-4 py-3 dark:bg-slate-900">
            <div className="augur-eyebrow">{label}</div>
            <div className="mt-1 mono text-sm font-semibold augur-strong">{value}</div>
          </div>
        ))}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th>Month</th>
              <th>Contribution</th>
              <th>Used</th>
              <th>Equity claim</th>
              <th>Ownership</th>
            </tr>
          </thead>
          <tbody>
            {annualRows.map((row) => {
              return (
                <tr key={row.monthIndex}>
                  <td>{row.monthIndex}</td>
                  <td>{fmtUsd(row.partnerContributionUsd)}</td>
                  <td>{fmtUsd(row.partnerContributionUsedUsd)}</td>
                  <td>{fmtUsd(row.partnerHomeEquityClaimUsd)}</td>
                  <td>{fmtPct(row.partnerOwnershipPct)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </ResultPanel>
  );
}

function LiquidityPolicyPanel({ trajectory }) {
  assertResultViewKind(trajectory, "trajectory");
  if (!trajectory.hasRows) return null;
  const annualRows = trajectory.annualRows();
  const terminalRow = trajectory.terminalRow();
  return (
    <ResultPanel kind="trajectory" title="Liquidity and stock sales">
      <div className="grid gap-px border-b border-slate-200 bg-slate-200 dark:border-slate-700 dark:bg-slate-700 sm:grid-cols-4">
        {[
          ["SP500 sales", fmtUsd(trajectory.sum("genericSp500SaleUsd"))],
          ["SP500 sale gain", fmtUsd(trajectory.sum("genericSp500SaleGainUsd"))],
          ["Final shortfall", fmtUsd(terminalRow?.checkingFloorShortfallUsd)],
          ["Final SP500", fmtUsd(terminalRow?.genericSp500ValueUsd)],
        ].map(([label, value]) => (
          <div key={label} className="bg-white px-4 py-3 dark:bg-slate-900">
            <div className="augur-eyebrow">{label}</div>
            <div className="mt-1 mono text-sm font-semibold augur-strong">{value}</div>
          </div>
        ))}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th>Year</th>
              <th>Cash</th>
              <th>SP500 value</th>
              <th>Sales</th>
              <th>Basis sold</th>
              <th>Gain</th>
              <th>Shortfall</th>
            </tr>
          </thead>
          <tbody>
            {annualRows.map((row) => (
              <tr key={row.monthIndex}>
                <td>{(row.monthIndex / 12).toFixed(0)}</td>
                <td>{fmtUsd(row.cashUsd)}</td>
                <td>{fmtUsd(row.genericSp500ValueUsd)}</td>
                <td>{fmtUsd(row.genericSp500SaleUsd)}</td>
                <td>{fmtUsd(row.genericSp500SaleBasisUsd)}</td>
                <td>{fmtUsd(row.genericSp500SaleGainUsd)}</td>
                <td>{fmtUsd(row.checkingFloorShortfallUsd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </ResultPanel>
  );
}

function PrivateEquitySaleOpportunityPanel({ trajectory }) {
  assertResultViewKind(trajectory, "trajectory");
  if (!trajectory.hasRows) return null;
  if (!trajectory.hasAny(["privateEquityValueUsd", "privateEquitySaleOpportunityValueUsd", "privateEquitySaleUsd"])) {
    return null;
  }
  const terminalRow = trajectory.terminalRow();
  const eventRows = trajectory.rowsWhere(
    (row) => row.privateEquitySaleOpportunityEvent || row.privateEquitySaleUsd > 0
  );
  const displayRows = (eventRows.length > 0 ? eventRows : trajectory.annualRows()).slice(0, 8);
  return (
    <ResultPanel kind="trajectory" title="Private equity tender opportunities">
      <div className="grid gap-px border-b border-slate-200 bg-slate-200 dark:border-slate-700 dark:bg-slate-700 sm:grid-cols-2">
        {[
          ["Private equity value", fmtUsd(terminalRow?.privateEquityValueUsd)],
          ["Sales", fmtUsd(trajectory.sum("privateEquitySaleUsd"))],
        ].map(([label, value]) => (
          <div key={label} className="bg-white px-4 py-3 dark:bg-slate-900">
            <div className="augur-eyebrow">{label}</div>
            <div className="mt-1 mono text-sm font-semibold augur-strong">{value}</div>
          </div>
        ))}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th>Month</th>
              <th>Private value</th>
              <th>Sale</th>
              <th>Tender event</th>
            </tr>
          </thead>
          <tbody>
            {displayRows.map((row) => (
              <tr key={row.monthIndex}>
                <td>{fmtInteger(row.monthIndex)}</td>
                <td>{fmtUsd(row.privateEquityValueUsd)}</td>
                <td>{fmtUsd(row.privateEquitySaleUsd)}</td>
                <td>{row.privateEquitySaleOpportunityEvent ? "yes" : "no"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </ResultPanel>
  );
}

function ScenarioList({ scenarioSetInput, selectedScenarioId, onSelect, onChange, bootstrap }) {
  const primary = bootstrap?.agents?.find((a) => a.role === "primary_owner");
  const partner = bootstrap?.agents?.find((a) => a.role === "equity_building_occupant");
  const primaryLabel = primary?.label ?? "Owner";
  const partnerLabel = partner?.label ?? "Partner";
  const scenarios = scenarioSetInput.scenarios;
  const propertiesById = useMemo(
    () => new Map(bootstrap.properties.map((property) => [property.id, property])),
    [bootstrap]
  );
  const locationsById = useMemo(
    () => new Map(bootstrap.locations.map((location) => [location.id, location])),
    [bootstrap]
  );

  function updateScenarioSection(scenarioId, section, patch) {
    onChange(scenarioSectionPatch(scenarioSetInput, scenarioId, section, patch));
  }

  function addScenario() {
    const scenarioId = uniqueScenarioId(scenarios.map(scenarioIdOf), "scenario");
    const nextScenario = createScenarioInput(bootstrap, {
      index: scenarios.length,
      scenarioId,
      label: `Scenario ${scenarios.length + 1}`,
    });
    onChange({ ...scenarioSetInput, scenarios: [...scenarios, nextScenario] });
    onSelect(scenarioId);
  }

  function duplicateScenario(scenarioIdToCopy) {
    const selected = scenarios.find((scenario) => scenarioIdOf(scenario) === scenarioIdToCopy) ?? scenarios[0];
    if (!selected) return;
    const selectedIdentity = scenarioIdentity(selected);
    const scenarioId = uniqueScenarioId(scenarios.map(scenarioIdOf), `${selectedIdentity.scenarioId}_copy`);
    const copy = patchScenarioInputSection(selected, "identity", {
      scenarioId,
      label: `${selectedIdentity.label} copy`,
      color: SCENARIO_COLORS[scenarios.length % SCENARIO_COLORS.length],
    });
    onChange({ ...scenarioSetInput, scenarios: [...scenarios, copy] });
    onSelect(scenarioId);
  }

  function deleteScenario(scenarioIdToDelete) {
    if (scenarios.length <= 1) return;
    const selected = scenarios.find((scenario) => scenarioIdOf(scenario) === scenarioIdToDelete);
    const label = scenarioIdentity(selected).label ?? "this scenario";
    if (!window.confirm(`Delete ${label}?`)) return;
    const nextScenarios = scenarios.filter((scenario) => scenarioIdOf(scenario) !== scenarioIdToDelete);
    onChange({ ...scenarioSetInput, scenarios: nextScenarios });
    onSelect(selectedScenarioId === scenarioIdToDelete ? (scenarioIdOf(nextScenarios[0]) ?? null) : selectedScenarioId);
  }

  return (
    <section className="augur-card overflow-hidden">
      <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="augur-eyebrow">Scenarios</div>
            <div className="text-sm augur-muted">Compare property, actor, occupancy, and policy choices.</div>
          </div>
          <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" size="xs" variant="default" onClick={addScenario}>
              Add
            </Button>
          </div>
        </div>
      </div>
      <div className="grid gap-2 p-3">
        {scenarios.map((scenario) => {
          const identity = scenarioIdentity(scenario);
          const propertyAndLocation = scenarioPropertyAndLocation(scenario);
          const actorsAndOwnership = scenarioActorsAndOwnership(scenario);
          const selected = identity.scenarioId === selectedScenarioId;
          const property = propertiesById.get(propertyAndLocation.propertyId);
          return (
            <div
              key={identity.scenarioId}
              className={`min-w-0 rounded-lg border p-3 ${
                selected
                  ? "border-blue-500 bg-blue-50 dark:border-blue-400 dark:bg-blue-950/30"
                  : "border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900"
              }`}
            >
              <div className="flex min-w-0 items-start gap-3">
                <button
                  type="button"
                  className="mt-1 h-4 w-4 shrink-0 rounded-full border border-slate-400"
                  style={{ backgroundColor: identity.color }}
                  aria-label={`Select ${identity.label}`}
                  onClick={() => onSelect(identity.scenarioId)}
                />
                <button
                  type="button"
                  className="min-w-0 flex-1 text-left"
                  onClick={() => onSelect(identity.scenarioId)}
                >
                  <div className="truncate text-sm font-semibold augur-strong">{identity.label}</div>
                  <div className="mt-1 truncate text-xs augur-muted">{propertyLabel(property, locationsById)}</div>
                </button>
                <div className="flex shrink-0 flex-col items-end gap-2">
                  <Checkbox
                    size="xs"
                    label="Include"
                    checked={identity.enabled}
                    onChange={(event) =>
                      updateScenarioSection(identity.scenarioId, "identity", { enabled: event.target.checked })
                    }
                  />
                  <div className="flex items-center gap-1">
                    <Button
                      type="button"
                      size="xs"
                      variant="default"
                      onClick={() => duplicateScenario(identity.scenarioId)}
                    >
                      Duplicate
                    </Button>
                    <CloseButton
                      type="button"
                      size="sm"
                      variant="subtle"
                      color="red"
                      aria-label={`Delete ${identity.label}`}
                      onClick={() => deleteScenario(identity.scenarioId)}
                      disabled={scenarios.length <= 1}
                    />
                  </div>
                </div>
              </div>
              <div className="mt-3 flex min-w-0 flex-wrap items-center gap-2">
                <label className="flex min-w-0 flex-1 items-center gap-2 text-xs augur-muted">
                  Color
                  <input
                    aria-label={`${identity.label} color`}
                    className="h-8 w-12 rounded border border-slate-300 bg-white p-0 dark:border-slate-600"
                    type="color"
                    value={identity.color}
                    onChange={(event) =>
                      updateScenarioSection(identity.scenarioId, "identity", { color: event.target.value })
                    }
                  />
                </label>
                <span className="min-w-0 max-w-full shrink-0 truncate rounded border border-slate-200 px-2 py-1 text-xs augur-muted dark:border-slate-700">
                  {actorsAndOwnership.actorPolicy === "owner_plus_partner"
                    ? `${partnerLabel} active`
                    : `${primaryLabel} only`}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function SelectedScenarioControls({ scenario, scenarioSetInput, onChange, bootstrap }) {
  if (!scenario) return null;
  const identity = scenarioIdentity(scenario);
  const propertyAndLocation = scenarioPropertyAndLocation(scenario);
  const actorsAndOwnership = scenarioActorsAndOwnership(scenario);
  const timeline = scenarioTimeline(scenario);
  const financing = scenarioFinancing(scenario);
  const occupancyAndRental = scenarioOccupancyAndRental(scenario);
  const propertyAssumptions = scenarioPropertyAssumptions(scenario);
  const taxAccounting = scenarioTaxAccounting(scenario);
  const initialBalanceSheet = scenarioInitialBalanceSheet(scenario);
  const policies = scenarioPolicies(scenario);
  const primary = bootstrap?.agents?.find((a) => a.role === "primary_owner");
  const partner = bootstrap?.agents?.find((a) => a.role === "equity_building_occupant");
  const primaryLabel = primary?.label ?? "Owner";
  const partnerLabel = partner?.label ?? "Partner";
  const usesCheckingFloorPolicy = scenarioUsesCheckingFloorPolicy(scenario);
  const concentratedHolding = primaryConcentratedHolding(bootstrap);
  const privateEquityLabel = concentratedHolding?.label ?? "Private equity";
  const privateEquityCurrentValueUsd = privateEquityValueUsdForUnits(bootstrap, initialBalanceSheet.privateEquityUnits);
  const privateEquityUnitPriceUsd = privateEquityCurrentUnitPriceUsd(bootstrap);
  const isCustomFinancing = financing.financingMode === "custom";
  const usesPrivateEquityLiquidFloorPolicy = policies.privateEquitySalePolicy === PRIVATE_EQUITY_LIQUID_FLOOR_POLICY_ID;
  const locationsById = useMemo(
    () => new Map(bootstrap.locations.map((location) => [location.id, location])),
    [bootstrap]
  );

  function updateScenarioSection(section, patch) {
    onChange(scenarioSectionPatch(scenarioSetInput, identity.scenarioId, section, patch));
  }

  return (
    <section className="augur-card space-y-5 px-4 py-4">
      <ControlSection title="Identity and property">
        <div className="augur-eyebrow">Selected scenario</div>
        <div className="mt-3 grid gap-3 sm:grid-cols-[minmax(0,1fr)_8.5rem]">
          <TextInput
            label="Label"
            value={identity.label}
            classNames={{ label: "augur-field-label mb-2 block" }}
            onChange={(event) => updateScenarioSection("identity", { label: event.target.value })}
          />
          <ColorInput
            label="Color"
            value={identity.color}
            format="hex"
            swatches={SCENARIO_COLORS}
            classNames={{ label: "augur-field-label mb-2 block", input: "augur-tabular" }}
            onChange={(color) => updateScenarioSection("identity", { color })}
          />
        </div>

        <div className="mt-3">
          <SelectField
            label="Scenario property"
            value={propertyAndLocation.propertyId}
            onChange={(propertyId) => updateScenarioSection("propertyAndLocation", { propertyId })}
            options={bootstrap.properties.map((property) => ({
              id: property.id,
              label: `${property.address} · ${propertyLocation(property, locationsById)?.label ?? property.locationId}`,
            }))}
          />
        </div>
      </ControlSection>

      <ControlSection title="Ownership and occupancy">
        <div className="grid gap-4">
          <OptionButtons
            label="Actors"
            options={bootstrap.actorPolicyOptions}
            value={actorsAndOwnership.actorPolicy}
            onChange={(actorPolicy) => updateScenarioSection("actorsAndOwnership", { actorPolicy })}
          />
          <OptionButtons
            label={`Where ${primaryLabel} lives`}
            options={bootstrap.ownerResidenceModeOptions}
            value={occupancyAndRental.ownerResidenceMode}
            onChange={(ownerResidenceMode) => updateScenarioSection("occupancyAndRental", { ownerResidenceMode })}
          />
          <OptionButtons
            label="Rental use"
            options={bootstrap.rentalUsePolicyOptions}
            value={occupancyAndRental.rentalUsePolicy}
            onChange={(rentalUsePolicy) => updateScenarioSection("occupancyAndRental", { rentalUsePolicy })}
          />
        </div>
      </ControlSection>

      <ControlSection title="Financing">
        <ControlGrid>
          <SelectField
            label="Financing mode"
            value={financing.financingMode}
            onChange={(financingMode) => updateScenarioSection("financing", { financingMode })}
            options={FINANCING_OPTIONS}
          />
          <NumberField
            label="Down payment"
            min={0}
            step={5}
            value={financing.downPaymentPct}
            onChange={(downPaymentPct) => updateScenarioSection("financing", { downPaymentPct })}
            suffix="%"
          />
          {isCustomFinancing && (
            <>
              <NumberField
                label="Custom mortgage rate"
                step={0.05}
                value={financing.customMortgageRate}
                onChange={(customMortgageRate) => updateScenarioSection("financing", { customMortgageRate })}
                suffix="%"
              />
              <NumberField
                label="Custom mortgage term"
                min={1}
                step={1}
                value={financing.customMortgageTermYears}
                onChange={(customMortgageTermYears) => updateScenarioSection("financing", { customMortgageTermYears })}
                suffix="yr"
              />
            </>
          )}
          <NumberField
            label="Credit score"
            min={300}
            step={1}
            value={financing.creditScore}
            onChange={(creditScore) => updateScenarioSection("financing", { creditScore })}
          />
          <NumberField
            label="Hold period"
            min={1}
            step={1}
            value={timeline.holdYears}
            onChange={(holdYears) => updateScenarioSection("timeline", { holdYears })}
            suffix="yr"
          />
        </ControlGrid>
      </ControlSection>

      <ControlSection title="Rental assumptions">
        <ControlGrid>
          <NumberField
            label="Vacancy"
            step={1}
            value={occupancyAndRental.vacancyPct}
            onChange={(vacancyPct) => updateScenarioSection("occupancyAndRental", { vacancyPct })}
            suffix="%"
          />
          <NumberField
            label="Management fee"
            step={0.5}
            value={occupancyAndRental.managementFeePct}
            onChange={(managementFeePct) => updateScenarioSection("occupancyAndRental", { managementFeePct })}
            suffix="%"
          />
          <NumberField
            label="Leasing fee"
            step={5}
            value={occupancyAndRental.leasingFeePct}
            onChange={(leasingFeePct) => updateScenarioSection("occupancyAndRental", { leasingFeePct })}
            suffix="%"
          />
          <NumberField
            label="Rooms rented while living"
            step={1}
            value={occupancyAndRental.roomsRentedWhileLiving}
            onChange={(roomsRentedWhileLiving) =>
              updateScenarioSection("occupancyAndRental", { roomsRentedWhileLiving })
            }
          />
          <MoneyField
            label="Room rent"
            step={50}
            value={occupancyAndRental.roomRentMonthlyUsd}
            onChange={(roomRentMonthlyUsd) => updateScenarioSection("occupancyAndRental", { roomRentMonthlyUsd })}
            suffix="/ mo"
          />
          <NumberField
            label="Room vacancy"
            step={1}
            value={occupancyAndRental.roomVacancyPct}
            onChange={(roomVacancyPct) => updateScenarioSection("occupancyAndRental", { roomVacancyPct })}
            suffix="%"
          />
        </ControlGrid>
      </ControlSection>

      <ControlSection title="Taxes and transaction costs">
        <ControlGrid>
          <NumberField
            label="Maintenance"
            step={0.1}
            value={propertyAssumptions.maintenancePct}
            onChange={(maintenancePct) => updateScenarioSection("propertyAssumptions", { maintenancePct })}
            suffix="%"
          />
          <MoneyField
            label="Insurance"
            step={100}
            value={propertyAssumptions.insuranceAnnualUsd}
            onChange={(insuranceAnnualUsd) => updateScenarioSection("propertyAssumptions", { insuranceAnnualUsd })}
            suffix="/ yr"
          />
          <NumberField
            label="Buy closing cost"
            step={0.1}
            value={taxAccounting.closingCostBuyPct}
            onChange={(closingCostBuyPct) => updateScenarioSection("taxAccounting", { closingCostBuyPct })}
            suffix="%"
          />
          <NumberField
            label="Sell closing cost"
            step={0.1}
            value={taxAccounting.closingCostSellPct}
            onChange={(closingCostSellPct) => updateScenarioSection("taxAccounting", { closingCostSellPct })}
            suffix="%"
          />
          <MoneyField
            label="Capital gains exclusion"
            step={50_000}
            value={taxAccounting.capGainsExclusionUsd}
            onChange={(capGainsExclusionUsd) => updateScenarioSection("taxAccounting", { capGainsExclusionUsd })}
          />
          <NumberField
            label="Depreciable basis"
            step={1}
            value={propertyAssumptions.depreciableBasisPct}
            onChange={(depreciableBasisPct) => updateScenarioSection("propertyAssumptions", { depreciableBasisPct })}
            suffix="%"
          />
          <NumberField
            label="Marginal tax rate"
            step={1}
            value={taxAccounting.marginalTaxRate}
            onChange={(marginalTaxRate) => updateScenarioSection("taxAccounting", { marginalTaxRate })}
            suffix="%"
          />
          <NumberField
            label="Capital gains rate"
            step={1}
            value={taxAccounting.capGainsRate}
            onChange={(capGainsRate) => updateScenarioSection("taxAccounting", { capGainsRate })}
            suffix="%"
          />
        </ControlGrid>
      </ControlSection>

      <ControlSection title="Portfolio and actors">
        <div className="grid gap-4">
          <PortfolioSnapshotPanel bootstrap={bootstrap} />
          <OptionButtons
            label="Reserve sales rule"
            options={bootstrap.liquidReservePolicyOptions}
            value={policies.liquidReservePolicy}
            onChange={(liquidReservePolicy) => updateScenarioSection("policies", { liquidReservePolicy })}
          />
          <ControlGrid>
            {usesCheckingFloorPolicy && (
              <>
                <MoneyField
                  label="Checking floor"
                  value={policies.checkingFloorUsd}
                  onChange={(checkingFloorUsd) => updateScenarioSection("policies", { checkingFloorUsd })}
                />
                <MoneyField
                  label="Sale amount"
                  min={1_000}
                  value={policies.checkingSaleAmountUsd}
                  onChange={(checkingSaleAmountUsd) => updateScenarioSection("policies", { checkingSaleAmountUsd })}
                />
              </>
            )}
            <MoneyField
              label="Initial checking"
              value={initialBalanceSheet.initialCheckingUsd}
              onChange={(initialCheckingUsd) => updateScenarioSection("initialBalanceSheet", { initialCheckingUsd })}
            />
            <MoneyField
              label="SP500-like portfolio"
              value={initialBalanceSheet.startingPortfolioUsd}
              onChange={(startingPortfolioUsd) =>
                updateScenarioSection("initialBalanceSheet", { startingPortfolioUsd })
              }
            />
            <ReadOnlyMetricField
              label={`${privateEquityLabel} value`}
              value={fmtUsd(privateEquityCurrentValueUsd)}
              detail={Number.isFinite(privateEquityUnitPriceUsd) ? `${fmtUsd(privateEquityUnitPriceUsd)} / unit` : null}
            />
            <NumberField
              label={`${privateEquityLabel} units`}
              step={1}
              value={initialBalanceSheet.privateEquityUnits}
              onChange={(privateEquityUnits) => updateScenarioSection("initialBalanceSheet", { privateEquityUnits })}
            />
          </ControlGrid>
          <OptionButtons
            label={`${privateEquityLabel} tender policy`}
            options={PRIVATE_EQUITY_SALE_POLICY_OPTIONS}
            value={policies.privateEquitySalePolicy}
            onChange={(privateEquitySalePolicy) => updateScenarioSection("policies", { privateEquitySalePolicy })}
          />
          {usesPrivateEquityLiquidFloorPolicy && (
            <ControlGrid>
              <MoneyField
                label="Liquid worth floor"
                value={policies.privateEquityLiquidNetWorthFloorUsd}
                onChange={(privateEquityLiquidNetWorthFloorUsd) =>
                  updateScenarioSection("policies", { privateEquityLiquidNetWorthFloorUsd })
                }
              />
              <MoneyField
                label="Tender sale amount"
                min={1_000}
                value={policies.privateEquityTenderSaleAmountUsd}
                onChange={(privateEquityTenderSaleAmountUsd) =>
                  updateScenarioSection("policies", { privateEquityTenderSaleAmountUsd })
                }
              />
            </ControlGrid>
          )}
          <ControlGrid>
            <MoneyField
              label={`${partnerLabel} payment`}
              step={50}
              value={actorsAndOwnership.partnerPaymentMonthlyUsd}
              onChange={(partnerPaymentMonthlyUsd) =>
                updateScenarioSection("actorsAndOwnership", { partnerPaymentMonthlyUsd })
              }
              suffix="/ mo"
            />
          </ControlGrid>
        </div>
      </ControlSection>
    </section>
  );
}

function RunStatusNotice({ runError }) {
  if (runError) {
    return <div className="augur-note-danger">Scenario-set run failed: {runError}</div>;
  }
  return null;
}

function ResultViewTabs({ viewMode, onViewModeChange }) {
  const options = [
    ["distribution", "Distribution"],
    ["trajectory", "Trajectory"],
  ];
  return (
    <Tabs
      value={viewMode}
      onChange={(mode) => {
        if (RESULT_VIEW_MODES.has(mode)) onViewModeChange(mode);
      }}
      classNames={{
        root: "inline-block max-w-full",
        list: "inline-flex max-w-full rounded-lg border border-slate-200 bg-white p-1 shadow-sm dark:border-slate-700 dark:bg-slate-900",
        tab: "augur-view-tab",
      }}
    >
      <Tabs.List>
        {options.map(([mode, label]) => (
          <Tabs.Tab key={mode} value={mode}>
            {label}
          </Tabs.Tab>
        ))}
      </Tabs.List>
    </Tabs>
  );
}

function ResultModeHeader({ viewMode, scenarioSetRequest, selection, selectedRolloutIndex }) {
  const isTrajectory = viewMode === "trajectory";
  const seed = scenarioSetRequest.marketRequest.seed;
  const kind = isTrajectory ? "trajectory" : "distribution";
  const rolloutStatus = isTrajectory ? rolloutStatusForIndex(selection.scenarioResult, selectedRolloutIndex) : null;
  return (
    <ResultPanel
      kind={kind}
      title={isTrajectory ? "Trajectory view" : "Distribution view"}
      subtitle={
        isTrajectory
          ? `Selected scenario · rollout ${fmtInteger(selectedRolloutIndex)} · ${selectedRolloutStatusText(rolloutStatus)} · seed ${seed}`
          : "Terminal percentiles and probability fans"
      }
    />
  );
}

function MultiScenarioFanChart({ scenarioSetInput, result, selectedMetric, onSelectedMetricChange }) {
  const metricOptions = metricOptionsFromResult(result, scenarioSetInput);
  const metricName = metricOptions.includes(selectedMetric) ? selectedMetric : (metricOptions[0] ?? "netWorthUsd");
  const series = scenarioSetInput.scenarios
    .map((scenario) => {
      const identity = scenarioIdentity(scenario);
      const rows = scenarioFanRows(result, identity.scenarioId, metricName);
      if (rows.length === 0) return null;
      return { scenario: identity, rows };
    })
    .filter(Boolean);
  if (series.length === 0) return null;

  const allRows = series.flatMap((item) => item.rows);
  const maxYear = Math.max(...allRows.map((row) => Number(row.year) || 0), 1);
  const values = allRows.flatMap((row) => [row.p05, row.p50, row.p95]).filter(Number.isFinite);
  const yAxis = fanChartAxis(metricName, values);
  const width = 760;
  const height = 260;
  const left = 72;
  const right = 24;
  const top = 20;
  const bottom = 42;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const x = (row) => left + ((Number(row.year) || 0) / maxYear) * plotWidth;
  const y = (value) => top + (1 - (value - yAxis.min) / yAxis.range) * plotHeight;
  const line = (rows, key) => rows.map((row) => `${x(row)},${y(row[key])}`).join(" ");
  const band = (rows) => {
    const upper = rows.map((row) => `${x(row)},${y(row.p95)}`).join(" ");
    const lower = rows
      .slice()
      .reverse()
      .map((row) => `${x(row)},${y(row.p05)}`)
      .join(" ");
    return `${upper} ${lower}`;
  };

  return (
    <ResultPanel
      kind="distribution"
      title="Scenario probability fans"
      subtitle={labelFromCamel(metricName)}
      actions={
        <NativeSelect
          aria-label="Fan metric"
          value={metricName}
          data={metricOptions.map((option) => ({ value: option, label: labelFromCamel(option) }))}
          className="min-w-[14rem]"
          onChange={(event) => onSelectedMetricChange(event.target.value)}
        />
      }
    >
      <div className="overflow-x-auto p-4">
        <svg
          role="img"
          aria-label={`Scenario comparison ${labelFromCamel(metricName)} probability fan chart`}
          viewBox={`0 0 ${width} ${height}`}
          className="min-w-[42rem] w-full"
        >
          <rect x={left} y={top} width={plotWidth} height={plotHeight} fill="transparent" />
          {yAxis.ticks.map((value) => {
            const yPos = y(value);
            return (
              <g key={value}>
                <line x1={left} x2={left + plotWidth} y1={yPos} y2={yPos} stroke="var(--augur-chart-grid)" />
                <text x={left - 8} y={yPos + 4} textAnchor="end" className="fill-slate-500 text-[11px] augur-tabular">
                  {fmtAxisMetricValue(metricName, value)}
                </text>
              </g>
            );
          })}
          {FAN_CHART_TICK_FRACTIONS.map((tick) => {
            const xPos = left + tick * plotWidth;
            return (
              <g key={tick}>
                <line x1={xPos} x2={xPos} y1={top} y2={top + plotHeight} stroke="var(--augur-chart-grid-subtle)" />
                <text x={xPos} y={height - 15} textAnchor="middle" className="fill-slate-500 text-[11px]">
                  {(tick * maxYear).toFixed(0)} yr
                </text>
              </g>
            );
          })}
          {series.map(({ scenario, rows }) => (
            <g key={scenario.scenarioId}>
              <polygon points={band(rows)} fill={scenario.color} opacity="0.12" />
              <polyline points={line(rows, "p50")} fill="none" stroke={scenario.color} strokeWidth="2.5" />
              <polyline points={line(rows, "p05")} fill="none" stroke={scenario.color} strokeWidth="1" opacity="0.45" />
              <polyline points={line(rows, "p95")} fill="none" stroke={scenario.color} strokeWidth="1" opacity="0.45" />
            </g>
          ))}
        </svg>
        <div className="mt-3 flex flex-wrap gap-3 text-xs augur-muted">
          {series.map(({ scenario }) => (
            <span key={scenario.scenarioId} className="inline-flex items-center gap-2">
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: scenario.color }} />
              {scenario.label}
            </span>
          ))}
        </div>
      </div>
    </ResultPanel>
  );
}

function ScenarioComparisonPanel({ scenarioSetInput, result, propertiesById }) {
  const scenarioResults = result?.scenarioResults ?? [];
  const showCheckingFloorColumns = scenarioSetUsesCheckingFloorPolicy(scenarioSetInput);
  const terminalMetricColumns = [
    ["finalNetWorthUsd", "P50 net worth"],
    ["finalLiquidNetWorthUsd", "P50 liquid net worth"],
    ["finalGenericSp500ValueUsd", "P50 SP500 value"],
    ["totalGenericSp500SaleUsd", "P50 SP500 sales"],
    ["finalCheckingFloorShortfallUsd", "P50 SP500 shortfall"],
    ["finalPrivateEquityValueUsd", "P50 private equity"],
    ["totalPrivateEquitySaleUsd", "P50 private equity sales"],
    ["finalHomeEquityUsd", "P50 home equity"],
    ["finalMortgageBalanceUsd", "P50 mortgage"],
    ["totalRentalIncomeUsd", "P50 rent income"],
    ["totalPropertyCarryingCostUsd", "P50 carry costs"],
    ["totalNetPropertyCashFlowUsd", "P50 net property cash flow"],
    ["totalPropertySaleNetProceedsUsd", "P50 sale net"],
    ["totalNetPropertySaleCashFlowUsd", "P50 sale cash flow"],
    ["totalPropertySaleTaxUsd", "P50 sale tax"],
    ["totalSaleClosingCostUsd", "P50 sale costs"],
    ["finalCumulativePropertyDepreciationUsd", "P50 cum. depreciation"],
    ["totalPartnerContributionUsedUsd", "P50 partner contrib."],
    ["finalPartnerHomeEquityClaimUsd", "P50 partner equity"],
    ["finalPartnerOwnershipPct", "P50 partner own."],
  ].filter(([column]) => showCheckingFloorColumns || !CHECKING_FLOOR_METRICS.has(column));
  return (
    <ResultPanel kind="distribution" title="Terminal scenario comparison">
      {result?.warnings?.length > 0 && (
        <div className="border-b border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
          {result.warnings.join(" ")}
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th className="text-left">Scenario</th>
              <th className="min-w-[11rem] text-left">Property</th>
              <th>Rollout health</th>
              {terminalMetricColumns.map(([, label]) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {scenarioSetInput.scenarios.map((scenario) => {
              const identity = scenarioIdentity(scenario);
              const propertyAndLocation = scenarioPropertyAndLocation(scenario);
              const scenarioResult = scenarioResults.find((item) => item.scenarioId === identity.scenarioId);
              const distribution = distributionResultView(scenarioResult);
              const fanRows = distribution.metricFanRows("netWorthUsd");
              const terminal = fanRows.length > 0 ? fanRows[fanRows.length - 1] : null;
              const property = propertiesById.get(propertyAndLocation.propertyId);
              const statusSummary = rolloutStatusSummary(scenarioResult);
              return (
                <tr key={identity.scenarioId}>
                  <td className="label">
                    <span className="inline-flex min-w-0 items-center gap-2">
                      <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: identity.color }} />
                      <span className="truncate">{identity.label}</span>
                    </span>
                  </td>
                  <td className="min-w-[11rem] whitespace-nowrap text-left">
                    {property ? property.address : propertyAndLocation.propertyId}
                  </td>
                  <RolloutHealthCell statusSummary={statusSummary} />
                  {terminalMetricColumns.map(([column]) => {
                    const value = column === "finalNetWorthUsd" ? terminal?.p50 : distribution.terminalP50(column);
                    return <td key={column}>{fmtMetricValue(column, value)}</td>;
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </ResultPanel>
  );
}

function MarketMetadataPanel({ result }) {
  const metadata = result?.marketMetadata;
  if (!metadata) return null;
  const sourceEntries = Object.entries(metadata.sourceMetadata ?? {});
  const metadataValue = (value) => (typeof value === "object" ? JSON.stringify(value) : String(value));
  return (
    <RunContextDisclosurePanel
      context="market-metadata"
      title="Market model metadata"
      summary={metadata.marketModelId ?? null}
    >
      <div className="grid gap-4 p-4 lg:grid-cols-2">
        <div className="min-w-0">
          <div className="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
            Event stream IDs
          </div>
          <div className="flex flex-wrap gap-2">
            {(metadata.eventStreamIds ?? []).map((eventStreamId) => (
              <span
                key={eventStreamId}
                className="rounded border border-slate-200 px-2 py-1 text-xs mono dark:border-slate-700"
              >
                {eventStreamId}
              </span>
            ))}
            {(metadata.eventStreamIds ?? []).length === 0 && <span className="text-sm augur-muted">none</span>}
          </div>
        </div>
        <div className="min-w-0">
          <div className="mb-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
            Source metadata
          </div>
          {sourceEntries.length > 0 ? (
            <dl className="grid gap-2">
              {sourceEntries.map(([key, value]) => (
                <div key={key} className="min-w-0 rounded-md border border-slate-200 p-2 dark:border-slate-700">
                  <dt className="text-xs font-semibold uppercase tracking-wide augur-muted">{labelFromCamel(key)}</dt>
                  <dd className="mt-1 max-h-20 overflow-auto break-all text-xs mono augur-strong">
                    {metadataValue(value)}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <div className="text-sm augur-muted">none</div>
          )}
        </div>
      </div>
    </RunContextDisclosurePanel>
  );
}

function LedgerTable({ rows, columns, className = "" }) {
  if (columns.length === 0) return null;
  return (
    <div className={`max-w-full overflow-auto ${className}`}>
      <table className="min-w-max">
        <thead className="sticky top-0 bg-white dark:bg-slate-900">
          <tr>
            <th>Month</th>
            {columns.map(([, label]) => (
              <th key={label}>{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.rolloutIndex}-${row.monthIndex}`}>
              <td>{fmtInteger(row.monthIndex)}</td>
              {columns.map(([column, , formatter]) => (
                <td key={column}>{formatter(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ledgerColumnExists(rows, column) {
  return rows.some((row) => row[column] !== undefined);
}

function filterLedgerColumns(rows, columns, showCheckingFloorColumns) {
  return columns.filter(
    ([column]) => ledgerColumnExists(rows, column) && (showCheckingFloorColumns || !CHECKING_FLOOR_METRICS.has(column))
  );
}

function LedgerDetailToggles({ groups, expandedGroups, onToggle }) {
  if (groups.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
      {groups.map((group) => {
        const expanded = !!expandedGroups[group.id];
        return (
          <button
            key={group.id}
            type="button"
            className={`rounded-md border px-2.5 py-1.5 text-xs font-semibold transition ${
              expanded
                ? "border-blue-500 bg-blue-50 text-blue-950 dark:border-blue-400 dark:bg-blue-950/40 dark:text-blue-100"
                : "border-slate-200 bg-white text-slate-600 hover:border-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
            }`}
            aria-pressed={expanded}
            onClick={() => onToggle(group.id)}
          >
            {group.label}
            <span className="ml-1 font-normal augur-muted">{expanded ? "details" : "total"}</span>
          </button>
        );
      })}
    </div>
  );
}

function ScenarioMonthlyLedger({ scenario, accountingDetail, onSelectedRolloutIndexChange }) {
  const [expandedLedgerGroups, setExpandedLedgerGroups] = useState({});
  const identity = scenarioIdentity(scenario);
  assertResultViewKind(accountingDetail, "accounting_detail");
  if (!scenario || !accountingDetail.hasRows) return null;
  const showCheckingFloorColumns = scenarioUsesCheckingFloorPolicy(scenario);
  const rolloutIndexes = accountingDetail.rolloutIndexes;
  const rolloutIndex = accountingDetail.rolloutIndex;
  const rows = accountingDetail.selectedRows;
  const rawGroups = [
    {
      id: "portfolio_sales",
      label: "SP500 sales",
      summary: ["genericSp500SaleUsd", "SP500 sales", fmtUsd],
      details: [
        ["genericSp500SaleUsd", "SP500 sales", fmtUsd],
        ["genericSp500SaleBasisUsd", "Basis sold", fmtUsd],
        ["genericSp500SaleGainUsd", "Gain", fmtUsd],
        ["genericSp500SaleTaxUsd", "Tax", fmtUsd],
        ["checkingFloorShortfallUsd", "Shortfall", fmtUsd],
      ],
    },
    {
      id: "private_equity_sale",
      label: "Private equity sale",
      summary: ["privateEquitySaleUsd", "PE sale", fmtUsd],
      details: [
        ["privateEquitySaleOpportunityValueUsd", "Sale opportunity value", fmtUsd],
        ["privateEquitySaleUsd", "Private equity sale", fmtUsd],
        ["privateEquitySaleBasisUsd", "Basis", fmtUsd],
        ["privateEquitySaleTaxUsd", "Tax", fmtUsd],
        ["privateEquitySaleOpportunityEvent", "Tender event", (value) => (value ? "yes" : "no")],
      ],
    },
    {
      id: "house_costs",
      label: "House costs",
      summary: ["propertyCarryingCostUsd", "House costs", fmtUsd],
      details: [
        ["propertyTaxUsd", "Property tax", fmtUsd],
        ["hoaUsd", "HOA", fmtUsd],
        ["insuranceUsd", "Insurance", fmtUsd],
        ["maintenanceUsd", "Maintenance", fmtUsd],
        ["propertyCarryingCostUsd", "House costs", fmtUsd],
      ],
    },
    {
      id: "rental_flow",
      label: "Rental flow",
      summary: ["netPropertyCashFlowUsd", "Property cash flow", fmtUsd],
      details: [
        ["rentalIncomeUsd", "Rent income", fmtUsd],
        ["rentalManagementFeeUsd", "Mgmt fee", fmtUsd],
        ["rentalLeasingFeeUsd", "Leasing fee", fmtUsd],
        ["mortgagePaymentUsd", "Mortgage pmt", fmtUsd],
        ["netPropertyCashFlowUsd", "Property cash flow", fmtUsd],
      ],
    },
    {
      id: "transaction_taxes",
      label: "Sale and taxes",
      summary: ["netPropertySaleCashFlowUsd", "Sale net", fmtUsd],
      details: [
        ["purchaseClosingCostUsd", "Buy costs", fmtUsd],
        ["propertyDepreciationUsd", "Depreciation", fmtUsd],
        ["cumulativePropertyDepreciationUsd", "Cum. depreciation", fmtUsd],
        ["propertySaleGrossUsd", "Sale gross", fmtUsd],
        ["propertySaleNetProceedsUsd", "Sale proceeds", fmtUsd],
        ["saleClosingCostUsd", "Sale costs", fmtUsd],
        ["propertySaleTaxUsd", "Sale tax", fmtUsd],
        ["propertySaleDebtPayoffUsd", "Debt payoff", fmtUsd],
        ["netPropertySaleCashFlowUsd", "Sale cash flow", fmtUsd],
        ["taxablePropertyGainUsd", "Taxable gain", fmtUsd],
      ],
    },
    {
      id: "partner_equity",
      label: "Partner equity",
      summary: ["partnerHomeEquityClaimUsd", "Partner equity", fmtUsd],
      details: [
        ["partnerContributionUsd", "Partner contrib.", fmtUsd],
        ["partnerContributionUsedUsd", "Partner used", fmtUsd],
        ["partnerUnallocatedExcessUsd", "Partner excess", fmtUsd],
        ["partnerHouseCostsUsd", "Partner costs", fmtUsd],
        ["partnerPrincipalCreditUsd", "Partner principal", fmtUsd],
        ["partnerHomeEquityClaimUsd", "Partner equity", fmtUsd],
        ["partnerOwnershipPct", "Partner own.", fmtPct],
      ],
    },
  ]
    .map((group) => ({
      ...group,
      summary: filterLedgerColumns(rows, [group.summary], showCheckingFloorColumns)[0],
      details: filterLedgerColumns(rows, group.details, showCheckingFloorColumns),
    }))
    .filter((group) => group.summary && group.details.length > 1);
  const groupById = new Map(rawGroups.map((group) => [group.id, group]));
  const columnsForGroup = (id) => {
    const group = groupById.get(id);
    if (!group) return [];
    return expandedLedgerGroups[id] ? group.details : [group.summary];
  };
  const ledgerColumns = [
    ["cashUsd", "Cash", fmtUsd],
    ["genericSp500ValueUsd", "SP500 value", fmtUsd],
    ...columnsForGroup("portfolio_sales"),
    ["privateEquityValueUsd", "Private equity", fmtUsd],
    ...columnsForGroup("private_equity_sale"),
    ["propertyValueUsd", "Property value", fmtUsd],
    ...columnsForGroup("house_costs"),
    ...columnsForGroup("rental_flow"),
    ["mortgageBalanceUsd", "Mortgage balance", fmtUsd],
    ["homeEquityUsd", "Home equity", fmtUsd],
    ...columnsForGroup("transaction_taxes"),
    ...columnsForGroup("partner_equity"),
    ["netWorthUsd", "Net worth", fmtUsd],
  ].filter(
    ([column], index, columns) =>
      ledgerColumnExists(rows, column) && columns.findIndex(([item]) => item === column) === index
  );
  const toggleLedgerGroup = (groupId) => {
    setExpandedLedgerGroups((previous) => ({ ...previous, [groupId]: !previous[groupId] }));
  };

  return (
    <ResultPanel
      kind="accounting_detail"
      title="Selected path monthly ledger"
      subtitle={`${identity.label} · rollout ${fmtInteger(rolloutIndex)}`}
      actions={
        <NativeSelect
          aria-label="Ledger path"
          value={String(rolloutIndex)}
          data={rolloutIndexes.map((index) => ({ value: String(index), label: `Path ${index}` }))}
          className="min-w-[10rem]"
          onChange={(event) => onSelectedRolloutIndexChange(Number(event.target.value))}
        />
      }
    >
      <LedgerDetailToggles groups={rawGroups} expandedGroups={expandedLedgerGroups} onToggle={toggleLedgerGroup} />
      <LedgerTable rows={rows} columns={ledgerColumns} className="max-h-[28rem]" />
    </ResultPanel>
  );
}

function ScenarioAcceptedPanel({ selection }) {
  const { scenario, scenarioResult } = selection;
  if (!scenario || !scenarioResult) return null;
  const propertyAndLocation = scenarioPropertyAndLocation(scenario);
  const actorsAndOwnership = scenarioActorsAndOwnership(scenario);
  return (
    <section className="augur-card overflow-hidden" data-scenario-context-panel="scenario-contract">
      <ResultPanelHeader title="Scenario contract" />
      <DetailTable
        rows={[
          ["Scenario id", scenarioResult.scenarioId],
          ["Enabled", scenarioResult.summary?.enabled ? "yes" : "no"],
          ["Property id", scenarioResult.summary?.propertyId ?? propertyAndLocation.propertyId],
          ["Location", scenarioResult.summary?.locationId ?? "n/a"],
          ["Participants", actorsAndOwnership.actorPolicy === "owner_plus_partner" ? "Owner + partner" : "Owner only"],
          ["Warnings", scenarioResult.warnings?.join("; ") || "none"],
        ]}
      />
    </section>
  );
}

function DistributionResults({
  scenarioSetRequest,
  result,
  runError,
  normalizedScenarioSetInput,
  propertiesById,
  selection,
  selectedFanMetric,
  onSelectedFanMetricChange,
}) {
  const { scenarioResult } = selection;
  const distribution = distributionResultView(scenarioResult);
  return (
    <>
      <RunStatusNotice runError={runError} />
      <ScenarioComparisonPanel
        scenarioSetInput={normalizedScenarioSetInput}
        result={result}
        propertiesById={propertiesById}
      />
      <MultiScenarioFanChart
        scenarioSetInput={normalizedScenarioSetInput}
        result={result}
        selectedMetric={selectedFanMetric}
        onSelectedMetricChange={onSelectedFanMetricChange}
      />
      <ScenarioValueSummary distribution={distribution} />
      <PropertySaleTaxDistributionPanel distribution={distribution} />
      <TerminalPercentileSnapshot distribution={distribution} />
    </>
  );
}

function TrajectoryResults({
  scenarioSetRequest,
  result,
  runError,
  selection,
  selectedRolloutIndex,
  onSelectedRolloutIndexChange,
  bootstrap,
}) {
  const { scenario, scenarioResult } = selection;
  const trajectory = trajectoryResultView(scenarioResult, selectedRolloutIndex);
  const accountingDetail = accountingDetailResultView(scenarioResult, selectedRolloutIndex);
  return (
    <>
      <RunStatusNotice runError={runError} />
      <ScenarioPathPreview trajectory={trajectory} />
      <ScenarioMonthlyLedger
        scenario={scenario}
        accountingDetail={accountingDetail}
        onSelectedRolloutIndexChange={onSelectedRolloutIndexChange}
      />
      <PartnerOwnershipPanel trajectory={trajectory} bootstrap={bootstrap} />
      <LiquidityPolicyPanel trajectory={trajectory} />
      <PrivateEquitySaleOpportunityPanel trajectory={trajectory} />
    </>
  );
}

function AugurAppShell() {
  const [bootstrap, setBootstrap] = useState(null);
  const [bootstrapError, setBootstrapError] = useState(null);
  const [urlStateError, setUrlStateError] = useState(null);
  const [scenarioSetInput, setScenarioSetInput] = useState(null);
  const [selectedScenarioId, setSelectedScenarioId] = useState(null);
  const [viewMode, setViewMode] = useState(() =>
    typeof window === "undefined" ? "distribution" : viewModeFromPathname(window.location.pathname)
  );
  const [selectedFanMetric, setSelectedFanMetric] = useState("netWorthUsd");
  const [selectedRolloutIndex, setSelectedRolloutIndex] = useState(0);
  const [result, setResult] = useState(null);
  const [runError, setRunError] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchAugurBootstrap({ signal: controller.signal })
      .then((payload) => {
        setBootstrap(payload);
        setBootstrapError(null);
        let initialInput = null;
        try {
          initialInput = typeof window === "undefined" ? null : scenarioSetInputFromUrlSearch(window.location.search);
          setUrlStateError(null);
        } catch (error) {
          setUrlStateError(error?.message || String(error));
        }
        const normalized = normalizeScenarioSetInput(initialInput ?? createDefaultScenarioSetInput(payload), payload);
        const requestedScenarioId = typeof window === "undefined" ? null : searchScenarioId(window.location.search);
        const selectedScenario = normalized.scenarios.some((scenario) => scenarioIdOf(scenario) === requestedScenarioId)
          ? requestedScenarioId
          : (scenarioIdOf(normalized.scenarios[0]) ?? null);
        setScenarioSetInput(normalized);
        setSelectedScenarioId(selectedScenario);
        setSelectedRolloutIndex(typeof window === "undefined" ? 0 : rolloutIndexFromSearch(window.location.search));
        setViewMode(typeof window === "undefined" ? "distribution" : viewModeFromPathname(window.location.pathname));
      })
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setBootstrapError(error?.message || String(error));
      });
    return () => controller.abort();
  }, []);

  const normalizedScenarioSetInput = useMemo(() => {
    if (!bootstrap || !scenarioSetInput) return null;
    return normalizeScenarioSetInput(scenarioSetInput, bootstrap);
  }, [bootstrap, scenarioSetInput]);

  const scenarioSetRequest = useMemo(() => {
    if (!bootstrap || !normalizedScenarioSetInput) return null;
    return scenarioSetInputToRequest(normalizedScenarioSetInput, bootstrap);
  }, [bootstrap, normalizedScenarioSetInput]);

  const propertiesById = useMemo(
    () => new Map((bootstrap?.properties ?? []).map((property) => [property.id, property])),
    [bootstrap]
  );
  const locationsById = useMemo(
    () => new Map((bootstrap?.locations ?? []).map((location) => [location.id, location])),
    [bootstrap]
  );

  const selectedContext = useMemo(() => {
    const scenario =
      normalizedScenarioSetInput?.scenarios.find((item) => scenarioIdOf(item) === selectedScenarioId) ?? null;
    const propertyAndLocation = scenarioPropertyAndLocation(scenario);
    const property = scenario ? (propertiesById.get(propertyAndLocation.propertyId) ?? null) : null;
    return {
      scenario,
      property,
      location: propertyLocation(property, locationsById),
      scenarioResult: scenarioResultById(result, selectedScenarioId),
    };
  }, [normalizedScenarioSetInput, selectedScenarioId, propertiesById, locationsById, result]);

  useEffect(() => {
    if (!normalizedScenarioSetInput) return;
    if (normalizedScenarioSetInput.scenarios.some((scenario) => scenarioIdOf(scenario) === selectedScenarioId)) return;
    setSelectedScenarioId(scenarioIdOf(normalizedScenarioSetInput.scenarios[0]) ?? null);
  }, [normalizedScenarioSetInput, selectedScenarioId]);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const syncFromLocation = () => {
      setViewMode(viewModeFromPathname(window.location.pathname));
      const scenarioId = searchScenarioId(window.location.search);
      if (scenarioId) setSelectedScenarioId(scenarioId);
      setSelectedRolloutIndex(rolloutIndexFromSearch(window.location.search));
    };
    window.addEventListener("popstate", syncFromLocation);
    return () => window.removeEventListener("popstate", syncFromLocation);
  }, []);

  useEffect(() => {
    if (!normalizedScenarioSetInput || typeof window === "undefined") return;
    const handle = setTimeout(() => {
      const nextSearch = searchWithAppState(
        window.location.search,
        normalizedScenarioSetInput,
        selectedScenarioId,
        selectedRolloutIndex
      );
      const nextUrl = `${pathForViewMode(viewMode)}${nextSearch}${window.location.hash}`;
      const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (nextUrl !== currentUrl) {
        window.history.replaceState(null, "", nextUrl);
      }
    }, 80);
    return () => clearTimeout(handle);
  }, [normalizedScenarioSetInput, selectedScenarioId, selectedRolloutIndex, viewMode]);

  useEffect(() => {
    if (!scenarioSetRequest) return;
    const controller = new AbortController();
    const handle = setTimeout(() => {
      runScenarioSet(scenarioSetRequest, { signal: controller.signal })
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
  }, [scenarioSetRequest]);

  if (!bootstrap || !normalizedScenarioSetInput || !scenarioSetRequest) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-100 text-sm augur-body dark:bg-slate-950">
        {bootstrapError ? (
          <div className="augur-note-danger max-w-lg p-4 text-sm">Augur bootstrap failed: {bootstrapError}</div>
        ) : (
          <div>Loading projection model...</div>
        )}
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-100 text-slate-800 dark:bg-slate-950 dark:text-slate-100">
      <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 px-4 py-3 shadow-sm backdrop-blur dark:border-slate-800 dark:bg-slate-950/95 sm:px-6 lg:px-8">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-base font-semibold text-slate-950 dark:text-slate-50">Augur</h1>
            <div className="text-xs uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
              Financial futures explorer
            </div>
          </div>
          <div className="flex min-w-[min(100%,24rem)] flex-1 flex-wrap items-center justify-end gap-3 text-xs augur-muted sm:flex-none">
            <span>{fmtNumber(scenarioSetRequest.marketRequest.horizonMonths)} month horizon</span>
          </div>
        </div>
      </header>

      <main className="px-4 py-6 sm:px-6 lg:px-8">
        {urlStateError && (
          <div className="mb-5 augur-note-danger">
            URL state could not be loaded; defaults are shown: {urlStateError}
          </div>
        )}
        <section className="grid min-w-0 gap-5 xl:grid-cols-[26rem_minmax(0,1fr)]">
          <aside className="min-w-0 space-y-5">
            <ScenarioList
              scenarioSetInput={normalizedScenarioSetInput}
              selectedScenarioId={selectedScenarioId}
              onSelect={setSelectedScenarioId}
              onChange={setScenarioSetInput}
              bootstrap={bootstrap}
            />
            <SelectedScenarioControls
              scenario={selectedContext.scenario}
              scenarioSetInput={normalizedScenarioSetInput}
              onChange={setScenarioSetInput}
              bootstrap={bootstrap}
            />
          </aside>

          <div className="min-w-0 space-y-5">
            <div className="border-b border-slate-300 pb-5 dark:border-slate-700">
              <div className="augur-eyebrow">
                {selectedContext.location?.label ?? "No property selected"} ·{" "}
                {selectedContext.location?.localRegulation
                  ? `${selectedContext.location.localRegulation.propertyTaxAnnualPct}% property tax`
                  : "no local regulation"}
              </div>
              <h2 className="display mt-2 text-3xl text-slate-950 dark:text-slate-50">
                {selectedContext.property?.address ?? "Scenario set"}
              </h2>
              <p className="mt-2 max-w-3xl augur-body">
                {selectedContext.property
                  ? `${selectedContext.property.neighborhood} · ${selectedContext.property.beds}bd · ${selectedContext.property.baths}ba · ${selectedContext.property.sqft.toLocaleString()} sf · ${fmtUsd(selectedContext.property.priceUsd)}`
                  : "Choose a property for the selected scenario."}
              </p>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3">
              <ResultViewTabs viewMode={viewMode} onViewModeChange={setViewMode} />
            </div>
            <PropertyLocationPanel selection={selectedContext} />
            <ScenarioFinancingTaxPanel selection={selectedContext} />
            <MarketMetadataPanel result={result} />
            <ScenarioAcceptedPanel selection={selectedContext} />
            <ResultModeHeader
              viewMode={viewMode}
              scenarioSetRequest={scenarioSetRequest}
              selection={selectedContext}
              selectedRolloutIndex={selectedRolloutIndex}
            />
            {viewMode === "trajectory" ? (
              <TrajectoryResults
                scenarioSetRequest={scenarioSetRequest}
                result={result}
                runError={runError}
                selection={selectedContext}
                selectedRolloutIndex={selectedRolloutIndex}
                onSelectedRolloutIndexChange={setSelectedRolloutIndex}
                bootstrap={bootstrap}
              />
            ) : (
              <DistributionResults
                scenarioSetRequest={scenarioSetRequest}
                result={result}
                runError={runError}
                normalizedScenarioSetInput={normalizedScenarioSetInput}
                propertiesById={propertiesById}
                selection={selectedContext}
                selectedFanMetric={selectedFanMetric}
                onSelectedFanMetricChange={setSelectedFanMetric}
              />
            )}
          </div>
        </section>
      </main>
    </div>
  );
}

export default function AugurApp() {
  return (
    <MantineProvider defaultColorScheme="auto">
      <AugurAppShell />
    </MantineProvider>
  );
}
