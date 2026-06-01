import React from "react";
import { fmtMetricValue } from "./lib/chart.ts";
import { FAN_PERCENTILES } from "./input_helpers.ts";
import { useCurrencyDisplay } from "./hooks.ts";
import {
  TABLE_NUMERIC_CELL,
  TABLE_NUMERIC_HEADER,
  SELECTED_COL_HEADER,
  SELECTED_COL_CELL,
  rolloutStatusText,
  terminalMetricTableRows,
} from "./data_helpers.ts";

export function TerminalMetricTable({ summaries, selectedSummary, metrics, selectedMetric }) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  if (summaries.length === 0) return null;
  const rows = terminalMetricTableRows(summaries, selectedSummary, metrics);
  // Determine where the SELECTED column slots into the percentile order based on the
  // currently-selected metric's selected value vs. its percentile distribution.
  const anchorRow = rows.find((row) => row.metric.value === selectedMetric?.value);
  const anchorValue = anchorRow?.selectedValue;
  const showSelectedColumn = selectedSummary != null && Number.isFinite(anchorValue);
  let selectedColumnIndex = FAN_PERCENTILES.length;
  if (showSelectedColumn) {
    const insertAt = anchorRow.percentiles.findIndex(({ value }) => Number.isFinite(value) && anchorValue < value);
    selectedColumnIndex = insertAt === -1 ? FAN_PERCENTILES.length : insertAt;
  }
  return (
    <div className="border-t border-slate-200 dark:border-slate-700">
      <div className="flex flex-col gap-1 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="augur-eyebrow">Terminal metrics</div>
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
              {FAN_PERCENTILES.map((percentile, index) => (
                <React.Fragment key={percentile}>
                  {showSelectedColumn && selectedColumnIndex === index && (
                    <th className={SELECTED_COL_HEADER}>Selected</th>
                  )}
                  <th className={TABLE_NUMERIC_HEADER}>P{percentile}</th>
                </React.Fragment>
              ))}
              {showSelectedColumn && selectedColumnIndex === FAN_PERCENTILES.length && (
                <th className={SELECTED_COL_HEADER}>Selected</th>
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {rows.map((row) => (
              <tr key={row.metric.value}>
                <th className="whitespace-nowrap px-4 py-2 text-left font-semibold text-slate-700 dark:text-slate-200">
                  {row.metric.label}
                </th>
                {row.percentiles.map(({ percentile, value }, index) => (
                  <React.Fragment key={percentile}>
                    {showSelectedColumn && selectedColumnIndex === index && (
                      <td className={SELECTED_COL_CELL}>
                        {fmtMetricValue(row.metric.chartValue, row.selectedValue, currencyDisplay)}
                      </td>
                    )}
                    <td className={TABLE_NUMERIC_CELL}>
                      {fmtMetricValue(row.metric.chartValue, value, currencyDisplay)}
                    </td>
                  </React.Fragment>
                ))}
                {showSelectedColumn && selectedColumnIndex === FAN_PERCENTILES.length && (
                  <td className={SELECTED_COL_CELL}>
                    {fmtMetricValue(row.metric.chartValue, row.selectedValue, currencyDisplay)}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
