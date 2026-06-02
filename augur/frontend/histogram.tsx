import React, { useCallback, useMemo, useRef } from "react";
import { axisCoordinate, fanChartAxis, fmtAxisMetricValue, fmtMetricValue } from "./lib/chart.ts";
import { FAN_PERCENTILES } from "./input_helpers.ts";
import { useCurrencyDisplay } from "./hooks.ts";
import {
  FAILED_ROLLOUT_COLOR,
  rolloutSliverColor,
  blendWithTeal,
  terminalHistogramBins,
  quantile,
  terminalMetricValue,
  rolloutStatusText,
} from "./data_helpers.ts";

export function TerminalDistributionHistogram({
  summaries,
  selectedSeed,
  loadingSeed,
  onSelect,
  metric,
  metricScale = "linear",
}) {
  // Hooks must run unconditionally, before the early return below. `entries` is safe to compute
  // for empty `summaries` (yields []), so it can live up here to feed the memo.
  const entries = summaries
    .map((summary) => ({ summary, value: terminalMetricValue(summary.terminalMetrics, metric) }))
    .filter((entry) => Number.isFinite(entry.value));
  const sortedEntries = useMemo(() => entries.slice().sort((left, right) => left.value - right.value), [entries]);
  const histogramDragRef = useRef({ dragging: false, startX: 0, startY: 0, startSeed: null, wasSelected: false });
  if (summaries.length === 0) return null;
  const axis =
    entries.length > 0
      ? fanChartAxis(
          metric.chartValue,
          entries.map((entry) => entry.value),
          metricScale
        )
      : { min: 0, max: 1, range: 1, ticks: [0, 1] };
  const binCount = Math.max(8, Math.min(36, Math.ceil(Math.sqrt(entries.length) * 1.3)));
  const bins = terminalHistogramBins(entries, binCount, axis.min, axis.max, (entry) =>
    axisCoordinate(axis, entry.value)
  );
  const maxBinCount = Math.max(...bins.map((bin) => bin.rollouts.length), 1);
  const cellHeight = Math.max(2, Math.min(10, Math.floor(280 / maxBinCount)));
  const containerHeight = Math.max(80, Math.min(320, cellHeight * maxBinCount + 4));
  const percentiles = FAN_PERCENTILES.map((percentile) => ({
    percentile,
    value: quantile(
      entries.filter((entry) => !entry.summary.failed).map((entry) => entry.value),
      percentile
    ),
  })).filter((row) => Number.isFinite(row.value));
  const axisLeftPct = (value) => {
    if (!Number.isFinite(value) || axis.range <= 0) return null;
    return ((axisCoordinate(axis, value) - axis.min) / axis.range) * 100;
  };
  const xTicks = Array.isArray(axis.ticks) ? axis.ticks.slice().sort((left, right) => left - right) : [];
  const selectedSliderEntry = sortedEntries.find((entry) => Number(entry.summary.seed) === selectedSeed) ?? null;
  const thumbLeftPct = selectedSliderEntry ? axisLeftPct(selectedSliderEntry.value) : null;
  const seedAtPoint = (clientX, clientY) => {
    const target = document.elementFromPoint(clientX, clientY);
    if (!target) return null;
    const cell = target.closest("[data-product-rollout-sliver]");
    if (!cell) return null;
    const seed = Number(cell.getAttribute("data-product-rollout-sliver"));
    return Number.isFinite(seed) ? seed : null;
  };
  const handleHistogramPointerDown = (event) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const seed = seedAtPoint(event.clientX, event.clientY);
    histogramDragRef.current = {
      dragging: true,
      startX: event.clientX,
      startY: event.clientY,
      startSeed: seed,
      wasSelected: seed != null && seed === selectedSeed,
    };
    if (seed != null && seed !== selectedSeed) onSelect(seed);
  };
  const handleHistogramPointerMove = (event) => {
    if (!histogramDragRef.current.dragging) return;
    const seed = seedAtPoint(event.clientX, event.clientY);
    if (seed != null && seed !== selectedSeed) onSelect(seed);
  };
  const handleHistogramPointerUp = (event) => {
    const state = histogramDragRef.current;
    histogramDragRef.current = { dragging: false, startX: 0, startY: 0, startSeed: null, wasSelected: false };
    event.currentTarget.releasePointerCapture(event.pointerId);
    // Preserve the original "click already-selected cell to deselect" behavior: if the press
    // started on the selected cell and never moved appreciably, treat the release as a toggle.
    if (!state.dragging || !state.wasSelected) return;
    const dx = event.clientX - state.startX;
    const dy = event.clientY - state.startY;
    if (dx * dx + dy * dy < 25) onSelect(null);
  };
  const handleHistogramPointerCancel = (event) => {
    histogramDragRef.current = { dragging: false, startX: 0, startY: 0, startSeed: null, wasSelected: false };
    event.currentTarget.releasePointerCapture(event.pointerId);
  };
  return (
    <div
      className="border-t border-slate-200 px-4 py-3 dark:border-slate-700"
      data-product-histogram-scale={metricScale}
    >
      <div className="mb-2 flex items-center justify-between gap-3">
        <div>
          <div className="augur-eyebrow">Terminal {metric.label.toLowerCase()} distribution</div>
          <div className="mt-1 text-xs augur-muted">One cell per rollout. Failures in red.</div>
        </div>
        {selectedSeed != null && (
          <button
            type="button"
            className="text-xs font-semibold text-blue-700 hover:text-blue-900 dark:text-blue-300 dark:hover:text-blue-200"
            onClick={() => onSelect(null)}
          >
            Clear
          </button>
        )}
      </div>
      <div className="flex items-stretch gap-3">
        <div className="relative flex flex-1 flex-col px-3">
          <div
            className="flex flex-1 cursor-pointer touch-none items-end gap-px"
            role="list"
            aria-label="Select rollout to inspect"
            style={{ height: containerHeight }}
            onPointerDown={handleHistogramPointerDown}
            onPointerMove={handleHistogramPointerMove}
            onPointerUp={handleHistogramPointerUp}
            onPointerCancel={handleHistogramPointerCancel}
          >
            {bins.map((bin) => (
              <TerminalHistogramColumn
                key={bin.lo}
                rollouts={bin.rollouts}
                cellHeight={cellHeight}
                containerHeight={containerHeight}
                selectedSeed={selectedSeed}
                loadingSeed={loadingSeed}
                onSelect={onSelect}
                metric={metric}
                cellColor={(entry) =>
                  entry.summary.failed ? FAILED_ROLLOUT_COLOR : rolloutSliverColor(entry.summary.rankPercentile)
                }
              />
            ))}
          </div>
          {percentiles.map(({ percentile, value }) => {
            const leftPct = axisLeftPct(value);
            if (leftPct == null) return null;
            return (
              <div
                key={percentile}
                className="pointer-events-none absolute inset-y-0"
                style={{ left: `${leftPct}%` }}
                aria-hidden="true"
              >
                <div className="absolute inset-y-0 w-px bg-slate-400/80 dark:bg-slate-300/40" />
                <div
                  className="absolute -translate-x-1/2 whitespace-nowrap text-[10px] font-semibold text-slate-500 dark:text-slate-400"
                  style={{ top: -14 }}
                >
                  P{percentile}
                </div>
              </div>
            );
          })}
          {thumbLeftPct != null && (
            <div
              className="pointer-events-none absolute inset-y-0 w-px bg-teal-500/80"
              style={{ left: `${thumbLeftPct}%` }}
              aria-hidden="true"
            />
          )}
          <div className="relative mt-1 h-4 text-[10px] augur-tabular augur-muted" aria-hidden="true">
            {xTicks.map((value) => {
              const leftPct = axisLeftPct(value);
              if (leftPct == null || leftPct < -1 || leftPct > 101) return null;
              return (
                <span
                  key={value}
                  className="absolute -translate-x-1/2 whitespace-nowrap"
                  style={{ left: `${leftPct}%` }}
                >
                  {fmtAxisMetricValue(metric.chartValue, value)}
                </span>
              );
            })}
          </div>
          <RolloutPercentileSlider
            sortedEntries={sortedEntries}
            axis={axis}
            axisLeftPct={axisLeftPct}
            selectedEntry={selectedSliderEntry}
            onSelect={onSelect}
            metric={metric}
          />
        </div>
      </div>
    </div>
  );
}

function RolloutPercentileSlider({ sortedEntries, axis, axisLeftPct, selectedEntry, onSelect, metric }) {
  const railRef = useRef(null);
  const draggingRef = useRef(false);
  const selectedIdx = selectedEntry ? sortedEntries.indexOf(selectedEntry) : -1;
  const thumbLeftPct = selectedEntry ? axisLeftPct(selectedEntry.value) : null;
  const valueLabel = selectedEntry ? fmtAxisMetricValue(metric.chartValue, selectedEntry.value) : null;
  const rankPercentile = selectedEntry ? Math.round(Number(selectedEntry.summary.rankPercentile)) : null;
  const failed = selectedEntry?.summary?.failed ?? false;

  const selectFromPointer = useCallback(
    (clientX) => {
      const rail = railRef.current;
      if (!rail || sortedEntries.length === 0) return;
      const rect = rail.getBoundingClientRect();
      if (rect.width <= 0) return;
      const t = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const targetCoord = axis.min + t * axis.range;
      let bestIdx = 0;
      let bestDist = Infinity;
      for (let idx = 0; idx < sortedEntries.length; idx += 1) {
        const coord = axisCoordinate(axis, sortedEntries[idx].value);
        const dist = Math.abs(coord - targetCoord);
        if (dist < bestDist) {
          bestDist = dist;
          bestIdx = idx;
        }
      }
      onSelect(Number(sortedEntries[bestIdx].summary.seed));
    },
    [sortedEntries, axis, onSelect]
  );

  const handlePointerDown = (event) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    draggingRef.current = true;
    selectFromPointer(event.clientX);
  };
  const handlePointerMove = (event) => {
    if (!draggingRef.current) return;
    selectFromPointer(event.clientX);
  };
  const handlePointerUp = (event) => {
    draggingRef.current = false;
    event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const handleKeyDown = (event) => {
    if (sortedEntries.length === 0) return;
    if (event.key === "Escape") {
      event.preventDefault();
      onSelect(null);
      return;
    }
    const step = event.shiftKey ? 10 : 1;
    let nextIdx;
    if (event.key === "ArrowRight") nextIdx = selectedIdx < 0 ? 0 : selectedIdx + step;
    else if (event.key === "ArrowLeft") nextIdx = selectedIdx < 0 ? sortedEntries.length - 1 : selectedIdx - step;
    else if (event.key === "Home") nextIdx = 0;
    else if (event.key === "End") nextIdx = sortedEntries.length - 1;
    else return;
    event.preventDefault();
    nextIdx = Math.max(0, Math.min(sortedEntries.length - 1, nextIdx));
    onSelect(Number(sortedEntries[nextIdx].summary.seed));
  };

  return (
    <div className="relative mt-3 select-none" data-product-percentile-slider>
      <div
        ref={railRef}
        role="slider"
        tabIndex={0}
        aria-label={`Inspect rollout by terminal ${metric.label.toLowerCase()}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={rankPercentile ?? 0}
        aria-valuetext={selectedEntry ? `P${rankPercentile}, ${valueLabel}` : "no rollout selected"}
        className="relative h-1.5 cursor-pointer rounded-full bg-slate-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-400 dark:bg-slate-700"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
        onKeyDown={handleKeyDown}
      >
        {thumbLeftPct != null && (
          <span
            aria-hidden="true"
            data-product-percentile-slider-thumb
            className="pointer-events-none absolute top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full bg-teal-500 shadow ring-2 ring-white dark:ring-slate-900"
            style={{ left: `${thumbLeftPct}%` }}
          />
        )}
      </div>
      <div className="relative mt-2 h-4 text-[11px]">
        {selectedEntry ? (
          <div
            className="absolute -translate-x-1/2 whitespace-nowrap font-semibold augur-tabular"
            style={{ left: `${thumbLeftPct}%` }}
          >
            P{rankPercentile} · {failed ? "failed" : valueLabel}
          </div>
        ) : (
          <div className="text-center augur-muted">Click a bar or drag the rail to inspect a rollout</div>
        )}
      </div>
    </div>
  );
}

export function TerminalHistogramColumn({
  rollouts,
  cellHeight,
  containerHeight,
  selectedSeed,
  loadingSeed,
  onSelect,
  cellColor,
  metric,
}) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  return (
    <div className="flex flex-1 flex-col-reverse items-stretch overflow-hidden" style={{ height: containerHeight }}>
      {rollouts.map((entry) => {
        const seed = Number(entry.summary.seed);
        const isSelected = selectedSeed === seed;
        const isLoading = loadingSeed === seed;
        const failedMonth = entry.summary.terminalMetrics?.failedMonthIndex;
        const valueLabel = Number.isFinite(entry.value)
          ? fmtMetricValue(metric.chartValue, entry.value, currencyDisplay)
          : "n/a";
        const titleParts = [
          `Seed ${seed}`,
          `P${Math.round(Number(entry.summary.rankPercentile))}`,
          rolloutStatusText(entry.summary),
          `terminal ${metric.label.toLowerCase()} ${valueLabel}`,
        ];
        return (
          <button
            key={seed}
            type="button"
            aria-label={titleParts.join(", ")}
            aria-pressed={isSelected}
            className="relative transition hover:brightness-125 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-teal-400"
            data-product-rollout-sliver={seed}
            onClick={() => onSelect(isSelected ? null : seed)}
            style={{
              height: cellHeight,
              backgroundColor: isSelected ? blendWithTeal(cellColor(entry)) : cellColor(entry),
            }}
            title={titleParts.join(" - ")}
          >
            {isLoading && (
              <span className="absolute inset-x-[30%] inset-y-[30%] rounded-full bg-teal-500" aria-hidden="true" />
            )}
            <span className="sr-only">
              {Number.isFinite(failedMonth) ? `failed in month ${failedMonth}` : rolloutStatusText(entry.summary)}
            </span>
          </button>
        );
      })}
    </div>
  );
}
