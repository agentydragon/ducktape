import React, { useCallback, useMemo, useRef, useState } from "react";
import { axisCoordinate, fanChartAxis, fmtAxisMetricValue, fmtMetricValue } from "./lib/chart";
import { rowsFrom } from "./lib/frame";
import { scenarioColor } from "./input_helpers";
import { useCurrencyDisplay } from "./hooks";
import { FAILED_ROLLOUT_COLOR, SELECTED_ROLLOUT_COLOR, terminalMetricSamples } from "./data_helpers";

function terminalPercentilePoints(result, metric) {
  if (result?.metric !== metric.value) return [];
  return rowsFrom(result?.terminalMetricPercentiles)
    .map((row) => ({
      percentile: Number(row.percentile) / 100,
      rawPercentile: Number(row.percentile),
      value: Number(row.value),
    }))
    .filter(
      (point) =>
        Number.isFinite(point.percentile) && Number.isFinite(point.rawPercentile) && Number.isFinite(point.value)
    )
    .sort((left, right) => left.percentile - right.percentile);
}

function valueAtPercentile(entry, percentile) {
  const points = entry.points;
  if (points.length === 0) return NaN;
  if (points.length === 1 || percentile <= points[0].percentile) return points[0].value;
  const last = points[points.length - 1];
  if (percentile >= last.percentile) return last.value;
  for (let index = 1; index < points.length; index += 1) {
    const right = points[index];
    if (percentile > right.percentile) continue;
    const left = points[index - 1];
    const span = right.percentile - left.percentile;
    if (span <= 0) return right.value;
    const weight = (percentile - left.percentile) / span;
    return left.value * (1 - weight) + right.value * weight;
  }
  return last.value;
}

function terminalFailedSamplePoints(result, metric) {
  const samples = terminalMetricSamples(result, metric)
    .slice()
    .sort((left, right) => left.value - right.value || left.seed - right.seed);
  if (samples.length === 0) return [];
  return samples
    .map((sample, index) => ({
      seed: sample.seed,
      percentile: samples.length === 1 ? 0.5 : index / (samples.length - 1),
      value: sample.value,
      failed: sample.failed,
    }))
    .filter((point) => point.failed);
}

function sampleAtPercentile(samples, percentile) {
  if (samples.length === 0) return null;
  if (samples.length === 1) return samples[0];
  const rank = Math.floor(Math.max(0, Math.min(1, percentile)) * (samples.length - 1) + 0.5);
  return samples[Math.max(0, Math.min(samples.length - 1, rank))];
}

// A click whose nearest line is farther than this (in px, on the Y axis) clears an existing
// selection instead of selecting. Gated on an existing selection so a first click anywhere still
// selects the nearest percentile.
const DESELECT_DISTANCE_PX = 30;

export function TerminalDistributionChart({
  scenarios,
  resultsById,
  activeId,
  selectedPercentile,
  loadingPercentile,
  onSelectPercentile,
  onClear,
  metric,
  metricScale = "linear",
}) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  const svgRef = useRef(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  const [svgWidth, setSvgWidth] = useState<number | null>(null);
  const [hoverPercentile, setHoverPercentile] = useState(null);
  const dragVariantRef = useRef({ dragging: false, variantId: null, startX: 0, startY: 0, wasSelected: false });

  // Callback ref, not a one-shot mount effect: the measured container is gated behind the early
  // `if (series.length === 0) return null` below, so it only mounts once results have loaded. A
  // `useEffect([])` runs after the *first* commit — when results often haven't arrived and the node
  // doesn't exist yet — bails, and (empty deps) never re-runs, leaving `svgWidth` null forever so the
  // plot never renders. A callback ref fires whenever the node actually attaches, however late.
  const measureContainer = useCallback((container: HTMLDivElement | null) => {
    resizeObserverRef.current?.disconnect();
    resizeObserverRef.current = null;
    if (!container) return;
    const update = () => {
      const style = window.getComputedStyle(container);
      const horizontalPadding = parseFloat(style.paddingLeft || "0") + parseFloat(style.paddingRight || "0");
      const containerWidth = container.getBoundingClientRect().width - horizontalPadding;
      const measuredSvgWidth = svgRef.current?.getBoundingClientRect().width ?? 0;
      const width = Math.max(containerWidth, measuredSvgWidth);
      setSvgWidth(Math.max(1, Math.round(width)));
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(container);
    resizeObserverRef.current = ro;
  }, []);

  // One percentile line per scenario, colored by position (matching the chips / fan legend), active
  // on top. Variants that haven't returned results yet contribute no line.
  const series = useMemo(
    () =>
      scenarios
        .map((scenario, index) => {
          const result = resultsById.get(scenario.id);
          const samples = terminalMetricSamples(result, metric).sort(
            (left, right) => left.value - right.value || left.seed - right.seed
          );
          return {
            id: scenario.id,
            label: scenario.label,
            color: scenarioColor(index),
            isActive: scenario.id === activeId,
            points: terminalPercentilePoints(result, metric),
            samples,
            failedPoints: terminalFailedSamplePoints(result, metric),
          };
        })
        .filter((entry) => entry.points.length > 0),
    [scenarios, resultsById, activeId, metric]
  );
  const orderedSeries = useMemo(
    () => [...series.filter((entry) => !entry.isActive), ...series.filter((entry) => entry.isActive)],
    [series]
  );

  if (series.length === 0) return null;

  const header = (
    <div className="mb-1 flex items-center justify-between gap-3">
      <div>
        <div className="augur-eyebrow">Terminal {metric.label.toLowerCase()} distribution</div>
        <div className="mt-1 text-xs augur-muted">
          One line per variant, drawn from aggregate terminal percentiles. Failed rollouts are marked.
        </div>
      </div>
      {selectedPercentile != null && (
        <button
          type="button"
          className="text-xs font-semibold text-blue-700 hover:text-blue-900 dark:text-blue-300 dark:hover:text-blue-200"
          onClick={onClear}
        >
          Clear
        </button>
      )}
    </div>
  );

  if (svgWidth == null) {
    return (
      <div
        ref={measureContainer}
        className="border-t border-slate-200 px-4 py-3 dark:border-slate-700"
        data-product-terminal-distribution=""
        data-product-distribution-scale={metricScale}
      >
        {header}
      </div>
    );
  }

  const allValues = series.flatMap((entry) => [
    ...entry.points.map((point) => point.value),
    ...entry.failedPoints.map((point) => point.value),
  ]);
  const yAxis = fanChartAxis(metric.chartValue, allValues, metricScale);
  const svgHeight = 260;
  const margin = { left: 82, right: 20, top: 16, bottom: 34 };
  const plotWidth = Math.max(1, svgWidth - margin.left - margin.right);
  const plotHeight = svgHeight - margin.top - margin.bottom;
  const xAt = (percentile) => margin.left + percentile * plotWidth;
  const yAt = (value) => margin.top + (1 - (axisCoordinate(yAxis, value) - yAxis.min) / yAxis.range) * plotHeight;

  const nearestPointIndex = (entry, percentile) => {
    let bestIndex = 0;
    let bestDistance = Infinity;
    for (let index = 0; index < entry.points.length; index += 1) {
      const distance = Math.abs(entry.points[index].percentile - percentile);
      if (distance < bestDistance) {
        bestDistance = distance;
        bestIndex = index;
      }
    }
    return bestIndex;
  };

  // Pick which variant a click/hover binds to: the line nearest the cursor in Y at the cursor's
  // percentile, breaking ties toward the active variant (drawn on top). `bestDist` is the distance
  // to the genuinely nearest line (pre-tie-break), used by the click-away-to-deselect check.
  const pickVariant = (percentile, cursorY) => {
    let best = null;
    let bestDist = Infinity;
    let active = null;
    let activeDist = Infinity;
    for (const entry of series) {
      const dist = Math.abs(cursorY - yAt(valueAtPercentile(entry, percentile)));
      if (entry.isActive) {
        active = entry;
        activeDist = dist;
      }
      if (dist < bestDist) {
        bestDist = dist;
        best = entry;
      }
    }
    return { entry: active && activeDist - bestDist <= 8 ? active : best, bestDist };
  };

  const localPoint = (event) => {
    const svg = svgRef.current;
    const rect = svg.getBoundingClientRect();
    const percentile = Math.max(0, Math.min(1, (event.clientX - rect.left - margin.left) / plotWidth));
    return { percentile, cursorY: event.clientY - rect.top };
  };

  const selectAt = (percentile, cursorY, variantId) => {
    const entry = variantId
      ? series.find((candidate) => candidate.id === variantId)
      : pickVariant(percentile, cursorY).entry;
    if (!entry) return null;
    const rawPercentile = Number((percentile * 100).toFixed(1));
    onSelectPercentile(entry.id, rawPercentile, sampleAtPercentile(entry.samples, percentile)?.seed ?? null);
    return entry.id;
  };

  const handlePointerDown = (event) => {
    if (event.clientX < 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const { percentile, cursorY } = localPoint(event);
    // Click well clear of every line clears an existing selection. Gated on `selectedPercentile` so a
    // first click anywhere still selects the nearest percentile.
    if (selectedPercentile != null && pickVariant(percentile, cursorY).bestDist > DESELECT_DISTANCE_PX) {
      onClear();
      dragVariantRef.current = { dragging: false, variantId: null, startX: 0, startY: 0, wasSelected: false };
      return;
    }
    const variantId = selectAt(percentile, cursorY, null);
    dragVariantRef.current = {
      dragging: true,
      variantId,
      startX: event.clientX,
      startY: event.clientY,
      // Track whether the press began on the already-selected percentile, to support click-to-deselect.
      wasSelected:
        variantId === activeId &&
        selectedPercentile != null &&
        Math.abs(selectedPercentile - Number((percentile * 100).toFixed(1))) < 0.5,
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
    // A click (negligible movement) on the already-selected percentile toggles it off.
    if (!state.dragging || !state.wasSelected) return;
    const dx = event.clientX - state.startX;
    const dy = event.clientY - state.startY;
    if (dx * dx + dy * dy < 25) onClear();
  };

  const activeSeries = series.find((entry) => entry.isActive) ?? null;
  const selectedFraction = selectedPercentile == null ? null : selectedPercentile / 100;
  const selectedValue =
    activeSeries && selectedFraction != null ? valueAtPercentile(activeSeries, selectedFraction) : NaN;
  const selectedPoint =
    activeSeries && selectedFraction != null && Number.isFinite(selectedValue)
      ? { percentile: selectedFraction, rawPercentile: selectedPercentile, value: selectedValue }
      : null;

  const moveSelection = (delta) => {
    if (!activeSeries) return;
    const base =
      selectedFraction == null
        ? delta > 0
          ? -1
          : activeSeries.points.length
        : nearestPointIndex(activeSeries, selectedFraction);
    const next = Math.max(0, Math.min(activeSeries.points.length - 1, base + delta));
    onSelectPercentile(activeSeries.id, activeSeries.points[next].rawPercentile);
  };
  const handleKeyDown = (event) => {
    const step = event.shiftKey ? 10 : 1;
    if (event.key === "ArrowRight") moveSelection(step);
    else if (event.key === "ArrowLeft") moveSelection(-step);
    else if (event.key === "Escape") onClear();
    else return;
    event.preventDefault();
  };

  // Hover tooltip enumerates every variant's value at the hovered percentile.
  const hoverRows =
    hoverPercentile == null
      ? []
      : series.map((entry) => {
          return {
            id: entry.id,
            label: entry.label,
            color: entry.color,
            value: valueAtPercentile(entry, hoverPercentile),
          };
        });
  const xTicks = [0, 0.25, 0.5, 0.75, 1];
  const tipWidth = 184;
  const tipHeight = 24 + hoverRows.length * 14;

  return (
    <div
      ref={measureContainer}
      className="border-t border-slate-200 px-4 py-3 dark:border-slate-700"
      data-product-terminal-distribution=""
      data-product-distribution-scale={metricScale}
    >
      {header}
      <svg
        ref={svgRef}
        role="slider"
        tabIndex={0}
        aria-label={`Inspect a rollout by terminal ${metric.label.toLowerCase()} percentile`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={selectedPercentile ?? 0}
        width={svgWidth}
        height={svgHeight}
        className="w-full cursor-pointer touch-none select-none focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
        data-product-terminal-distribution-plot=""
        data-product-terminal-distribution-rendered-width={svgWidth}
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
          const linePoints = entry.points.map((point) => `${xAt(point.percentile)},${yAt(point.value)}`).join(" ");
          return (
            <g
              key={entry.id}
              data-product-distribution-series={entry.id}
              data-product-distribution-point-count={entry.points.length}
            >
              <polyline
                points={linePoints}
                fill="none"
                stroke={entry.color}
                strokeWidth={entry.isActive ? 2.75 : 2}
                opacity={entry.isActive ? 1 : 0.85}
              />
            </g>
          );
        })}
        {orderedSeries.flatMap((entry) =>
          entry.failedPoints.map((point) => (
            <circle
              key={`${entry.id}:${point.seed}`}
              cx={xAt(point.percentile)}
              cy={yAt(point.value)}
              r={entry.isActive ? 3.75 : 3}
              fill={FAILED_ROLLOUT_COLOR}
              stroke="white"
              strokeWidth="1"
              opacity={entry.isActive ? 0.95 : 0.75}
              pointerEvents="none"
              data-product-distribution-failed=""
              data-product-distribution-failed-seed={point.seed}
            />
          ))
        )}
        {selectedPoint && (
          <>
            <line
              x1={xAt(selectedPoint.percentile)}
              x2={xAt(selectedPoint.percentile)}
              y1={margin.top}
              y2={margin.top + plotHeight}
              stroke="rgba(100,116,139,0.5)"
              strokeWidth="1"
              strokeDasharray="3 2"
            />
            <circle
              cx={xAt(selectedPoint.percentile)}
              cy={yAt(selectedPoint.value)}
              r="5"
              fill={SELECTED_ROLLOUT_COLOR}
              stroke="white"
              strokeWidth="1.5"
              data-product-distribution-selected={selectedPoint.rawPercentile}
            />
            {loadingPercentile != null && Math.abs(loadingPercentile - selectedPoint.rawPercentile) < 0.05 && (
              <circle
                cx={xAt(selectedPoint.percentile)}
                cy={yAt(selectedPoint.value)}
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
                      {row.label}: {fmtMetricValue(metric.chartValue, row.value, currencyDisplay)}
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
