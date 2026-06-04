import React, { useEffect, useMemo, useRef, useState } from "react";
import { axisCoordinate, fanChartAxis, fmtAxisMetricValue, fmtMetricValue } from "./lib/chart.ts";
import { scenarioColor } from "./input_helpers.ts";
import { useCurrencyDisplay } from "./hooks.ts";
import { FAILED_ROLLOUT_COLOR, SELECTED_ROLLOUT_COLOR, terminalMetricValue } from "./data_helpers.ts";

// Sort each variant's rollouts by outcome: failures first (earliest bust = worst = leftmost,
// ordered by failure month), then survivors ascending by terminal value. Failures plot at 0 so they
// read as a flat red floor whose width is the failure rate; survivors rise from there. Every variant
// shares the same seed set, so a seed is the *same underlying world* across variants — selection is
// therefore a seed, and switching the active variant relocates the marker to that seed's rank in the
// newly-active line without clearing it.
function orderedRollouts(summaries, metric) {
  const points = summaries.map((summary) => ({
    seed: Number(summary.seed),
    failed: Boolean(summary.failed),
    failedMonth: summary.terminalMetrics?.failedMonthIndex,
    value: terminalMetricValue(summary.terminalMetrics, metric),
  }));
  const failed = points
    .filter((point) => point.failed)
    .sort((left, right) => (left.failedMonth ?? 0) - (right.failedMonth ?? 0));
  const survived = points
    .filter((point) => !point.failed && Number.isFinite(point.value))
    .sort((left, right) => left.value - right.value);
  // A failed rollout's terminal value is post-bust and not meaningful, so plot it at 0 regardless of
  // what the array carried. CLEANUP tracked in augur/TODO.md: "failed = 0" is wrong for non-money
  // metrics (mortgage balance, property value) — revisit per-metric.
  return [...failed, ...survived].map((point) => ({ ...point, plotted: point.failed ? 0 : point.value }));
}

export function TerminalDistributionChart({
  scenarios,
  resultsById,
  activeId,
  selectedSeed,
  loadingSeed,
  onSelectRollout,
  onClear,
  metric,
  metricScale = "linear",
}) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  const svgRef = useRef(null);
  const [svgWidth, setSvgWidth] = useState(760);
  const [hoverPercentile, setHoverPercentile] = useState(null);
  const dragVariantRef = useRef({ dragging: false, variantId: null, startX: 0, startY: 0, wasSelected: false });

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const update = () => setSvgWidth(svg.clientWidth || 760);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(svg);
    return () => ro.disconnect();
  }, []);

  // One ordered line per scenario, colored by position (matching the chips / fan legend), active on
  // top. Variants that haven't returned results yet contribute no line.
  const series = useMemo(
    () =>
      scenarios
        .map((scenario, index) => ({
          id: scenario.id,
          label: scenario.label,
          color: scenarioColor(index),
          isActive: scenario.id === activeId,
          ordered: orderedRollouts(resultsById.get(scenario.id)?.rolloutSummaries ?? [], metric),
        }))
        .filter((entry) => entry.ordered.length > 0),
    [scenarios, resultsById, activeId, metric]
  );
  const orderedSeries = useMemo(
    () => [...series.filter((entry) => !entry.isActive), ...series.filter((entry) => entry.isActive)],
    [series]
  );

  if (series.length === 0) return null;

  const allPlotted = series.flatMap((entry) => entry.ordered.map((point) => point.plotted));
  const yAxis = fanChartAxis(metric.chartValue, allPlotted, metricScale);
  const svgHeight = 260;
  const margin = { left: 82, right: 20, top: 16, bottom: 34 };
  const plotWidth = Math.max(1, svgWidth - margin.left - margin.right);
  const plotHeight = svgHeight - margin.top - margin.bottom;
  // X is percentile within each variant's own rollouts (rank / (n-1)); a single-rollout variant pins
  // to the left edge. Per-variant percentile keeps lines comparable even if a variant is still
  // loading with fewer results than the others.
  const percentileOf = (entry, index) => (entry.ordered.length > 1 ? index / (entry.ordered.length - 1) : 0);
  const xAt = (percentile) => margin.left + percentile * plotWidth;
  const yAt = (value) => margin.top + (1 - (axisCoordinate(yAxis, value) - yAxis.min) / yAxis.range) * plotHeight;

  const indexAtPercentile = (entry, percentile) =>
    Math.max(0, Math.min(entry.ordered.length - 1, Math.round(percentile * (entry.ordered.length - 1))));

  // Pick which variant a click/hover binds to: the line nearest the cursor in Y at the cursor's
  // percentile, breaking ties toward the active variant (drawn on top) so overlapping failed floors
  // resolve to the variant you're already inspecting.
  const pickVariant = (percentile, cursorY) => {
    let best = null;
    let bestDist = Infinity;
    let active = null;
    let activeDist = Infinity;
    for (const entry of series) {
      const point = entry.ordered[indexAtPercentile(entry, percentile)];
      const dist = Math.abs(cursorY - yAt(point.plotted));
      if (entry.isActive) {
        active = entry;
        activeDist = dist;
      }
      if (dist < bestDist) {
        bestDist = dist;
        best = entry;
      }
    }
    return active && activeDist - bestDist <= 8 ? active : best;
  };

  const localPoint = (event) => {
    const svg = svgRef.current;
    const rect = svg.getBoundingClientRect();
    const percentile = Math.max(0, Math.min(1, (event.clientX - rect.left - margin.left) / plotWidth));
    return { percentile, cursorY: event.clientY - rect.top };
  };

  const selectAt = (percentile, cursorY, variantId) => {
    const entry = variantId ? series.find((candidate) => candidate.id === variantId) : pickVariant(percentile, cursorY);
    if (!entry) return null;
    const seed = entry.ordered[indexAtPercentile(entry, percentile)].seed;
    onSelectRollout(entry.id, seed);
    return entry.id;
  };

  const handlePointerDown = (event) => {
    if (event.clientX < 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const { percentile, cursorY } = localPoint(event);
    const variantId = selectAt(percentile, cursorY, null);
    dragVariantRef.current = {
      dragging: true,
      variantId,
      startX: event.clientX,
      startY: event.clientY,
      // Track whether the press began on the already-selected rollout, to support click-to-deselect.
      wasSelected:
        variantId === activeId &&
        series.find((entry) => entry.id === variantId)?.ordered[
          indexAtPercentile(
            series.find((entry) => entry.id === variantId),
            percentile
          )
        ]?.seed === selectedSeed,
    };
  };

  const handlePointerMove = (event) => {
    const { percentile, cursorY } = localPoint(event);
    setHoverPercentile(percentile);
    // While dragging, lock to the variant chosen on press and scrub the rank, so the selection
    // doesn't hop between overlapping lines mid-drag.
    if (dragVariantRef.current.dragging) selectAt(percentile, cursorY, dragVariantRef.current.variantId);
  };

  const handlePointerUp = (event) => {
    const state = dragVariantRef.current;
    dragVariantRef.current = { dragging: false, variantId: null, startX: 0, startY: 0, wasSelected: false };
    event.currentTarget.releasePointerCapture(event.pointerId);
    // A click (negligible movement) on the already-selected rollout toggles it off.
    if (!state.dragging || !state.wasSelected) return;
    const dx = event.clientX - state.startX;
    const dy = event.clientY - state.startY;
    if (dx * dx + dy * dy < 25) onClear();
  };

  const activeSeries = series.find((entry) => entry.isActive) ?? null;
  const selectedIndex =
    activeSeries && selectedSeed != null ? activeSeries.ordered.findIndex((point) => point.seed === selectedSeed) : -1;
  const selectedPoint = selectedIndex >= 0 ? activeSeries.ordered[selectedIndex] : null;

  const moveSelection = (delta) => {
    if (!activeSeries) return;
    const base = selectedIndex < 0 ? (delta > 0 ? -1 : activeSeries.ordered.length) : selectedIndex;
    const next = Math.max(0, Math.min(activeSeries.ordered.length - 1, base + delta));
    onSelectRollout(activeSeries.id, activeSeries.ordered[next].seed);
  };
  const handleKeyDown = (event) => {
    const step = event.shiftKey ? 10 : 1;
    if (event.key === "ArrowRight") moveSelection(step);
    else if (event.key === "ArrowLeft") moveSelection(-step);
    else if (event.key === "Escape") onClear();
    else return;
    event.preventDefault();
  };

  // Hover tooltip enumerates every variant's value at the hovered percentile, so even sitting over an
  // overlapping failed floor you read the list rather than guessing which line is under the cursor.
  const hoverRows =
    hoverPercentile == null
      ? []
      : series.map((entry) => {
          const point = entry.ordered[indexAtPercentile(entry, hoverPercentile)];
          return {
            id: entry.id,
            label: entry.label,
            color: entry.color,
            failed: point.failed,
            failedMonth: point.failedMonth,
            value: point.plotted,
          };
        });
  const xTicks = [0, 0.25, 0.5, 0.75, 1];
  const tipWidth = 184;
  const tipHeight = 24 + hoverRows.length * 14;

  return (
    <div
      className="border-t border-slate-200 px-4 py-3 dark:border-slate-700"
      data-product-terminal-distribution=""
      data-product-distribution-scale={metricScale}
    >
      <div className="mb-1 flex items-center justify-between gap-3">
        <div>
          <div className="augur-eyebrow">Terminal {metric.label.toLowerCase()} distribution</div>
          <div className="mt-1 text-xs augur-muted">
            One line per variant, rollouts sorted by outcome. Failures sit at 0 on the left. Click to inspect a rollout.
          </div>
        </div>
        {selectedSeed != null && (
          <button
            type="button"
            className="text-xs font-semibold text-blue-700 hover:text-blue-900 dark:text-blue-300 dark:hover:text-blue-200"
            onClick={onClear}
          >
            Clear
          </button>
        )}
      </div>
      <svg
        ref={svgRef}
        role="slider"
        tabIndex={0}
        aria-label={`Inspect a rollout by terminal ${metric.label.toLowerCase()} percentile`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={selectedIndex >= 0 ? Math.round(percentileOf(activeSeries, selectedIndex) * 100) : 0}
        height={svgHeight}
        className="w-full cursor-pointer touch-none select-none focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
        data-product-terminal-distribution-plot=""
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
        onPointerLeave={() => setHoverPercentile(null)}
        onKeyDown={handleKeyDown}
      >
        {yAxis.ticks.map((value) => {
          const yPos = yAt(value);
          if (!Number.isFinite(yPos) || yPos < margin.top - 1 || yPos > margin.top + plotHeight + 1) return null;
          return (
            <g key={value}>
              <line
                x1={margin.left}
                x2={margin.left + plotWidth}
                y1={yPos}
                y2={yPos}
                stroke="var(--augur-chart-grid)"
              />
              <text
                x={margin.left - 8}
                y={yPos + 4}
                textAnchor="end"
                className="fill-slate-500 text-[11px] augur-tabular"
              >
                {fmtAxisMetricValue(metric.chartValue, value)}
              </text>
            </g>
          );
        })}
        {xTicks.map((percentile) => {
          const xPos = xAt(percentile);
          return (
            <g key={percentile}>
              <line
                x1={xPos}
                x2={xPos}
                y1={margin.top}
                y2={margin.top + plotHeight}
                stroke="var(--augur-chart-grid-subtle)"
              />
              <text x={xPos} y={svgHeight - 14} textAnchor="middle" className="fill-slate-500 text-[11px]">
                {`P${Math.round(percentile * 100)}`}
              </text>
            </g>
          );
        })}
        {orderedSeries.map((entry) => {
          const failedCount = entry.ordered.filter((point) => point.failed).length;
          const failedFloor = entry.ordered
            .slice(0, failedCount)
            .map((point, index) => `${xAt(percentileOf(entry, index))},${yAt(0)}`)
            .join(" ");
          // The survivor curve includes the last failed point (at 0) as its left anchor so the line
          // rises continuously off the floor instead of leaving a gap at the lift-off rank.
          const survivorFrom = Math.max(0, failedCount - 1);
          const survivorLine = entry.ordered
            .slice(survivorFrom)
            .map((point, offset) => `${xAt(percentileOf(entry, survivorFrom + offset))},${yAt(point.plotted)}`)
            .join(" ");
          return (
            <g key={entry.id} data-product-distribution-series={entry.id}>
              {failedCount > 0 && (
                <polyline
                  points={failedFloor}
                  fill="none"
                  stroke={FAILED_ROLLOUT_COLOR}
                  strokeWidth={entry.isActive ? 3 : 2}
                  strokeLinecap="round"
                  opacity={entry.isActive ? 1 : 0.85}
                />
              )}
              {failedCount < entry.ordered.length && (
                <polyline
                  points={survivorLine}
                  fill="none"
                  stroke={entry.color}
                  strokeWidth={entry.isActive ? 2.75 : 2}
                  opacity={entry.isActive ? 1 : 0.85}
                />
              )}
            </g>
          );
        })}
        {selectedPoint && (
          <>
            <line
              x1={xAt(percentileOf(activeSeries, selectedIndex))}
              x2={xAt(percentileOf(activeSeries, selectedIndex))}
              y1={margin.top}
              y2={margin.top + plotHeight}
              stroke="rgba(100,116,139,0.5)"
              strokeWidth="1"
              strokeDasharray="3 2"
            />
            <circle
              cx={xAt(percentileOf(activeSeries, selectedIndex))}
              cy={yAt(selectedPoint.plotted)}
              r="5"
              fill={selectedPoint.failed ? FAILED_ROLLOUT_COLOR : SELECTED_ROLLOUT_COLOR}
              stroke="white"
              strokeWidth="1.5"
              data-product-distribution-selected={selectedSeed}
            />
            {loadingSeed === selectedSeed && (
              <circle
                cx={xAt(percentileOf(activeSeries, selectedIndex))}
                cy={yAt(selectedPoint.plotted)}
                r="8"
                fill="none"
                stroke={SELECTED_ROLLOUT_COLOR}
                strokeWidth="2"
                opacity="0.6"
              />
            )}
          </>
        )}
        {hoverPercentile != null &&
          (() => {
            const guideX = xAt(hoverPercentile);
            const tipX = guideX + 8 + tipWidth <= svgWidth ? 8 : -8 - tipWidth;
            return (
              <>
                <line
                  x1={guideX}
                  x2={guideX}
                  y1={margin.top}
                  y2={margin.top + plotHeight}
                  stroke="rgba(100,116,139,0.45)"
                  strokeWidth="1"
                  strokeDasharray="3 2"
                />
                <g transform={`translate(${guideX + tipX}, ${margin.top})`} className="pointer-events-none">
                  <rect
                    x={0}
                    y={0}
                    width={tipWidth}
                    height={tipHeight}
                    rx={4}
                    fill="white"
                    fillOpacity="0.94"
                    stroke="rgba(15,23,42,0.15)"
                    strokeWidth="1"
                  />
                  <text x={8} y={14} fontSize="10" fontWeight="600" fill="#334155">
                    P{Math.round(hoverPercentile * 100)}
                  </text>
                  {hoverRows.map((row, index) => (
                    <text key={row.id} x={8} y={28 + index * 14} fontSize="10" fill="#64748b">
                      <tspan fill={row.color} fontWeight="700">
                        ●{" "}
                      </tspan>
                      {row.label}:{" "}
                      {row.failed
                        ? `failed${Number.isFinite(row.failedMonth) ? ` m${row.failedMonth}` : ""}`
                        : fmtMetricValue(metric.chartValue, row.value, currencyDisplay)}
                    </text>
                  ))}
                </g>
              </>
            );
          })()}
      </svg>
    </div>
  );
}
