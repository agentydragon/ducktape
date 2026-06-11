import React from "react";
import { fmtMetricValue } from "./lib/chart.ts";
import { FAN_PERCENTILES, scenarioColor } from "./input_helpers.ts";
import { useCurrencyDisplay } from "./hooks.ts";
import {
  TABLE_NUMERIC_CELL,
  TABLE_NUMERIC_HEADER,
  SELECTED_COL_HEADER,
  SELECTED_COL_CELL,
  rolloutStatusText,
  terminalMetricValue,
  terminalPercentileValue,
} from "./data_helpers.ts";

export function TerminalMetricTable({ result, selectedSummary, selectedMetric }) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  if (!result?.terminalMetricPercentiles) return null;
  const percentileRows = FAN_PERCENTILES.map((percentile) => ({
    percentile,
    value: terminalPercentileValue(result, percentile),
  })).filter((row) => Number.isFinite(row.value));
  if (percentileRows.length === 0) return null;
  // Determine where the SELECTED column slots into the percentile order based on the
  // currently-selected metric's selected value vs. its percentile distribution.
  const selectedValue = selectedSummary ? terminalMetricValue(selectedSummary.terminalMetrics, selectedMetric) : null;
  const anchorValue = selectedValue;
  const showSelectedColumn = selectedSummary != null && Number.isFinite(anchorValue);
  let selectedColumnIndex = percentileRows.length;
  if (showSelectedColumn) {
    const insertAt = percentileRows.findIndex(({ value }) => Number.isFinite(value) && anchorValue < value);
    selectedColumnIndex = insertAt === -1 ? percentileRows.length : insertAt;
  }
  return (
    <div className="border-t border-slate-200 dark:border-slate-700">
      <div className="flex flex-col gap-1 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="augur-eyebrow">Terminal {selectedMetric.label.toLowerCase()}</div>
          <div className="mt-1 text-xs augur-muted">
            Distribution percentiles with the selected rollout beside them.
          </div>
        </div>
        <div className="text-xs font-semibold augur-tabular augur-muted">
          {selectedSummary
            ? `Seed ${selectedSummary.seed} - ${rolloutStatusText(selectedSummary)}`
            : "No rollout selected"}
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full border-t border-slate-200 text-sm dark:border-slate-700">
          <thead>
            <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
              <th className="px-4 py-2 font-semibold">Metric</th>
              {percentileRows.map(({ percentile }, index) => (
                <React.Fragment key={percentile}>
                  {showSelectedColumn && selectedColumnIndex === index && (
                    <th className={SELECTED_COL_HEADER}>Selected</th>
                  )}
                  <th className={TABLE_NUMERIC_HEADER}>P{percentile}</th>
                </React.Fragment>
              ))}
              {showSelectedColumn && selectedColumnIndex === percentileRows.length && (
                <th className={SELECTED_COL_HEADER}>Selected</th>
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            <tr>
              <th className="whitespace-nowrap px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                {selectedMetric.label}
              </th>
              {percentileRows.map(({ percentile, value }, index) => (
                <React.Fragment key={percentile}>
                  {showSelectedColumn && selectedColumnIndex === index && (
                    <td className={SELECTED_COL_CELL}>
                      {fmtMetricValue(selectedMetric.chartValue, selectedValue, currencyDisplay)}
                    </td>
                  )}
                  <td className={TABLE_NUMERIC_CELL}>
                    {fmtMetricValue(selectedMetric.chartValue, value, currencyDisplay)}
                  </td>
                </React.Fragment>
              ))}
              {showSelectedColumn && selectedColumnIndex === percentileRows.length && (
                <td className={SELECTED_COL_CELL}>
                  {fmtMetricValue(selectedMetric.chartValue, selectedValue, currencyDisplay)}
                </td>
              )}
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Compact comparison across the scenario set for the metric currently being plotted. The fan
// endpoint is aggregate-only, so this intentionally reads each scenario's terminal percentile frame
// instead of per-rollout summaries. Hidden for a lone scenario.
export function TerminalScenarioComparison({ scenarios, resultsById, metric, activeId }) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  if (scenarios.length <= 1) return null;
  const columns = scenarios.map((scenario, index) => ({
    scenario,
    color: scenarioColor(index),
    isActive: scenario.id === activeId,
    result: resultsById.get(scenario.id)?.metric === metric.value ? resultsById.get(scenario.id) : null,
  }));
  if (columns.every((column) => !column.result?.terminalMetricPercentiles)) return null;
  return (
    <div className="border-t border-slate-200 dark:border-slate-700" data-product-scenario-comparison="">
      <div className="px-4 py-3">
        <div className="augur-eyebrow">Terminal scenario comparison</div>
        <div className="mt-1 text-xs augur-muted">
          Median terminal {metric.label.toLowerCase()} per scenario, with the P5-P95 range below.
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full border-t border-slate-200 text-sm dark:border-slate-700">
          <thead>
            <tr className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
              <th className="px-4 py-2 font-semibold">Metric</th>
              {columns.map((column) => (
                <th
                  key={column.scenario.id}
                  className={`${TABLE_NUMERIC_HEADER}${column.isActive ? " bg-slate-100/80 dark:bg-slate-800/60" : ""}`}
                  // Underline the active column in its own scenario color so it reads as the entity
                  // the histogram / selected-rollout / events panels below are all scoped to.
                  style={column.isActive ? { borderBottom: `2px solid ${column.color}` } : undefined}
                  data-product-scenario-comparison-col={column.scenario.id}
                  data-active={column.isActive ? "" : undefined}
                >
                  <span className="inline-flex items-center justify-end gap-1.5">
                    <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: column.color }} />
                    <span className={column.isActive ? "font-semibold text-slate-700 dark:text-slate-200" : ""}>
                      {column.scenario.label}
                    </span>
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            <tr>
              <th className="whitespace-nowrap px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                {metric.label}
              </th>
              {columns.map((column) => (
                <td
                  key={column.scenario.id}
                  className={`${TABLE_NUMERIC_CELL}${column.isActive ? " bg-slate-100/60 dark:bg-slate-800/40" : ""}`}
                  data-active={column.isActive ? "" : undefined}
                >
                  <div className="font-semibold">
                    {fmtMetricValue(metric.chartValue, terminalPercentileValue(column.result, 50), currencyDisplay)}
                  </div>
                  <div className="text-[11px] augur-muted">
                    {fmtMetricValue(metric.chartValue, terminalPercentileValue(column.result, 5), currencyDisplay)} -{" "}
                    {fmtMetricValue(metric.chartValue, terminalPercentileValue(column.result, 95), currencyDisplay)}
                  </div>
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
