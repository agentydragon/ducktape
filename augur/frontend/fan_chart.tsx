import React, { useEffect, useRef, useState } from "react";
import { axisCoordinate, fanChartAxis, fanChartYearTicks, fmtAxisMetricValue, fmtMetricValue } from "./lib/chart.ts";
import { useCurrencyDisplay } from "./hooks.ts";
import {
  SELECTED_ROLLOUT_COLOR,
  FAILED_ROLLOUT_COLOR,
  EVENT_MARKER_STACK_PITCH_PX,
  EVENT_MARKER_STACK_BASE_OFFSET_PX,
  eventMonthIndex,
  eventColor,
  eventTitle,
} from "./data_helpers.ts";

function FanAxes({ left, top, plotWidth, plotHeight, height, y, yAxis, maxYear, metric }) {
  return (
    <>
      {yAxis.ticks.map((value) => {
        const yPos = y(value);
        return (
          <g key={value}>
            <line x1={left} x2={left + plotWidth} y1={yPos} y2={yPos} stroke="var(--augur-chart-grid)" />
            <text x={left - 8} y={yPos + 4} textAnchor="end" className="fill-slate-500 text-[11px] augur-tabular">
              {fmtAxisMetricValue(metric.chartValue, value)}
            </text>
          </g>
        );
      })}
      {fanChartYearTicks(maxYear).map((year) => {
        const xPos = left + (year / maxYear) * plotWidth;
        return (
          <g key={year}>
            <line x1={xPos} x2={xPos} y1={top} y2={top + plotHeight} stroke="var(--augur-chart-grid-subtle)" />
            <text x={xPos} y={height - 15} textAnchor="middle" className="fill-slate-500 text-[11px]">
              {year} yr
            </text>
          </g>
        );
      })}
    </>
  );
}

function FanEventMarker({
  event,
  index,
  monthIndex,
  row,
  color,
  stackIndex,
  stackCount,
  x,
  y,
  top,
  plotHeight,
  selectedEventMonthIndex,
  hoveredEventMonthIndex,
  onSelectEventMonth,
  onHoverEventMonth,
}) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  const title = eventTitle(event, currencyDisplay);
  const isSelected = selectedEventMonthIndex === monthIndex;
  const isHovered = hoveredEventMonthIndex === monthIndex;
  const isActive = isSelected || isHovered;
  const markerX = x(row);
  // Stack the dots vertically *above* the rollout line, in the order the events arrive
  // (which `decode.py:rollout_events_from` already sorts by `priority[kind]`). The vertical
  // guide line still anchors at the row's value so the user can read the month at a glance.
  const stackOffset = EVENT_MARKER_STACK_BASE_OFFSET_PX - stackIndex * EVENT_MARKER_STACK_PITCH_PX;
  const markerY = Math.max(top + 6, Math.min(top + plotHeight - 6, y(row.value) + stackOffset));
  const baseRadius = 4.5;
  const radius = isActive ? baseRadius + 2.2 : baseRadius;
  // Only the top-most marker in a stack draws the vertical guide line; otherwise every event
  // would render an overlapping line at the same x. The line still appears whenever any
  // member of the stack is hovered/selected because the activeness propagates per-month.
  const drawsGuideLine = stackIndex === stackCount - 1;
  return (
    <g
      key={`${event.kind}-${event.monthIndex}-${index}`}
      role="button"
      tabIndex={0}
      aria-label={title}
      data-product-rollout-event-marker={event.kind}
      data-product-rollout-event-marker-month={monthIndex}
      data-product-rollout-event-marker-selected={isSelected ? "true" : "false"}
      data-product-rollout-event-marker-hovered={isHovered ? "true" : "false"}
      onClick={() => onSelectEventMonth?.(monthIndex)}
      onKeyDown={(keyboardEvent) => {
        if (keyboardEvent.key !== "Enter" && keyboardEvent.key !== " ") return;
        keyboardEvent.preventDefault();
        onSelectEventMonth?.(monthIndex);
      }}
      onMouseEnter={() => onHoverEventMonth?.(monthIndex)}
      onMouseLeave={() => onHoverEventMonth?.(null)}
      onFocus={() => onHoverEventMonth?.(monthIndex)}
      onBlur={() => onHoverEventMonth?.(null)}
      style={{ cursor: "pointer" }}
    >
      {drawsGuideLine && (
        <line
          x1={markerX}
          x2={markerX}
          y1={top}
          y2={top + plotHeight}
          stroke="var(--augur-chart-grid)"
          opacity={isActive ? 0.46 : 0.2}
          strokeWidth={isActive ? 1.6 : 1}
        />
      )}
      {isActive && (
        <circle
          cx={markerX}
          cy={markerY}
          r={radius + 3}
          fill="none"
          stroke={isSelected ? SELECTED_ROLLOUT_COLOR : "#0891b2"}
          strokeWidth="2"
          opacity="0.72"
        />
      )}
      <circle
        cx={markerX}
        cy={markerY}
        r={radius}
        fill={color}
        opacity={0.98}
        stroke="white"
        strokeWidth={isActive ? 2 : 1.25}
      >
        <title>{title}</title>
      </circle>
    </g>
  );
}

// `series` is one entry per scenario: `{ id, label, color, rows, isActive }` where `rows` is the
// metric fan (`{ monthIndex, year, values: Map<percentile, value> }[]`). The selected-rollout
// overlay and event markers always belong to the active scenario (the caller passes its
// `selectedRows`/`selectedEvents`).
export function MetricFanChart({
  series,
  metric,
  percentiles,
  selectedRows,
  selectedEvents,
  selectedSeed,
  selectedFailed,
  visibleEventKinds,
  selectedEventMonthIndex,
  hoveredEventMonthIndex,
  onSelectEventMonth,
  onHoverEventMonth,
  metricScale = "linear",
}) {
  const { display: currencyDisplay } = useCurrencyDisplay();
  const [hoveredMonth, setHoveredMonth] = useState(null);
  const svgRef = useRef(null);
  const [svgWidth, setSvgWidth] = useState(760);
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const update = () => setSvgWidth(svg.clientWidth || 760);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(svg);
    return () => ro.disconnect();
  }, []);

  // A lone scenario renders exactly like the pre-comparison chart: outer + inner band, median, and
  // P5/P95 edge lines, all in blue. Two or more switch to one P5–P95 band + median per scenario,
  // each in its own color, dropping the inner band and edge lines so the overlay stays readable.
  const single = series.length <= 1;
  const allRows = series.flatMap((entry) => entry.rows);
  if (allRows.length === 0) return null;
  const activeSeries = series.find((entry) => entry.isActive) ?? series[0];

  const sortedPercentiles = percentiles.slice().sort((left, right) => left - right);
  const outerLow = sortedPercentiles[0];
  const outerHigh = sortedPercentiles[sortedPercentiles.length - 1];
  const innerLow = sortedPercentiles[Math.min(1, sortedPercentiles.length - 1)];
  const innerHigh = sortedPercentiles[Math.max(0, sortedPercentiles.length - 2)];
  const median = sortedPercentiles.includes(50) ? 50 : sortedPercentiles[Math.floor(sortedPercentiles.length / 2)];
  const maxYear = Math.max(...allRows.map((row) => row.year), 1);
  const values = allRows
    .flatMap((row) => sortedPercentiles.map((pct) => row.values.get(pct)))
    .concat(selectedRows.map((row) => row.value))
    .filter(Number.isFinite);
  const yAxis = fanChartAxis(metric.chartValue, values, metricScale);
  const svgHeight = 300;
  const margin = { left: 82, right: 24, top: 18, bottom: 42 };
  const plotHeight = svgHeight - margin.top - margin.bottom;
  const plotWidth = svgWidth - margin.left - margin.right;
  const x = (row) => margin.left + (row.year / maxYear) * plotWidth;
  const y = (value) => margin.top + (1 - (axisCoordinate(yAxis, value) - yAxis.min) / yAxis.range) * plotHeight;
  const valueAt = (row, pct) => row.values.get(pct);
  const line = (rows, pct) => rows.map((row) => `${x(row)},${y(valueAt(row, pct))}`).join(" ");
  const band = (rows, upperPct, lowerPct) => {
    const upper = rows.map((row) => `${x(row)},${y(valueAt(row, upperPct))}`).join(" ");
    const lower = rows
      .slice()
      .reverse()
      .map((row) => `${x(row)},${y(valueAt(row, lowerPct))}`)
      .join(" ");
    return `${upper} ${lower}`;
  };
  const selectedLine = selectedRows.map((row) => `${x(row)},${y(row.value)}`).join(" ");
  const selectedColor = selectedFailed ? FAILED_ROLLOUT_COLOR : SELECTED_ROLLOUT_COLOR;
  const selectedRowByMonth = new Map(selectedRows.map((row) => [row.monthIndex, row]));

  // Inactive scenarios render first so the active scenario's band + median sit on top.
  const orderedSeries = [...series.filter((entry) => !entry.isActive), ...series.filter((entry) => entry.isActive)];
  // Per-scenario month→row lookup for the comparison tooltip (each scenario's median at the hovered month).
  const rowByMonthBySeries = new Map();
  for (const entry of series) {
    rowByMonthBySeries.set(entry.id, new Map(entry.rows.map((row) => [row.monthIndex, row])));
  }

  const monthBuckets = new Map();
  for (let index = 0; index < selectedEvents.length; index += 1) {
    const event = selectedEvents[index];
    if (!visibleEventKinds.has(event.kind)) continue;
    const monthIndex = eventMonthIndex(event);
    if (monthIndex == null) continue;
    const row = selectedRowByMonth.get(monthIndex);
    if (!row) continue;
    if (!monthBuckets.has(monthIndex)) monthBuckets.set(monthIndex, []);
    monthBuckets.get(monthIndex).push({ event, index, row });
  }
  const eventMarkers = [];
  for (const [monthIndex, bucket] of monthBuckets) {
    for (let stackIndex = 0; stackIndex < bucket.length; stackIndex += 1) {
      const { event, index, row } = bucket[stackIndex];
      eventMarkers.push({
        event,
        index,
        monthIndex,
        row,
        color: eventColor(event),
        stackIndex,
        stackCount: bucket.length,
      });
    }
  }

  // Plain handlers (not useCallback): they sit after the early return above, and as DOM event
  // handlers gain nothing from memoization.
  const handleMouseMove = (event) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const svgX = event.clientX - rect.left;
    if (svgX < margin.left || svgX > margin.left + plotWidth) {
      setHoveredMonth(null);
      return;
    }
    const targetYear = ((svgX - margin.left) / plotWidth) * maxYear;
    let closest = null;
    let closestDist = Infinity;
    for (const row of activeSeries.rows) {
      const dist = Math.abs(row.year - targetYear);
      if (dist < closestDist) {
        closestDist = dist;
        closest = row;
      }
    }
    setHoveredMonth(closest);
  };

  const handleMouseLeave = () => setHoveredMonth(null);

  const hoveredRow = hoveredMonth;

  return (
    <div className="overflow-x-auto p-4" data-product-fan-chart={metric.chartValue} data-product-scale={metricScale}>
      {!single && (
        <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" data-product-fan-legend="">
          {series.map((entry) => (
            <span key={entry.id} className="inline-flex items-center gap-1.5" data-product-fan-legend-item={entry.id}>
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: entry.color }} />
              <span
                className={
                  entry.isActive
                    ? "font-semibold text-slate-700 dark:text-slate-200"
                    : "text-slate-500 dark:text-slate-400"
                }
              >
                {entry.label}
              </span>
            </span>
          ))}
        </div>
      )}
      <svg
        ref={svgRef}
        role="img"
        aria-label={`${metric.label} probability fan chart`}
        height={svgHeight}
        className="w-full"
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
      >
        <rect x={margin.left} y={margin.top} width={plotWidth} height={plotHeight} fill="transparent" />
        <FanAxes
          left={margin.left}
          top={margin.top}
          plotWidth={plotWidth}
          plotHeight={plotHeight}
          height={svgHeight}
          y={y}
          yAxis={yAxis}
          maxYear={maxYear}
          metric={metric}
        />
        {single ? (
          <>
            <polygon points={band(activeSeries.rows, outerHigh, outerLow)} fill="#2563eb" opacity="0.14" />
            <polygon points={band(activeSeries.rows, innerHigh, innerLow)} fill="#2563eb" opacity="0.22" />
            <polyline points={line(activeSeries.rows, median)} fill="none" stroke="#1d4ed8" strokeWidth="2.75" />
            <polyline
              points={line(activeSeries.rows, outerLow)}
              fill="none"
              stroke="#1d4ed8"
              strokeWidth="1"
              opacity="0.45"
            />
            <polyline
              points={line(activeSeries.rows, outerHigh)}
              fill="none"
              stroke="#1d4ed8"
              strokeWidth="1"
              opacity="0.45"
            />
          </>
        ) : (
          <>
            {orderedSeries.map((entry) => (
              <React.Fragment key={`bounds-${entry.id}`}>
                <polyline
                  points={line(entry.rows, outerLow)}
                  fill="none"
                  stroke={entry.color}
                  strokeWidth={entry.isActive ? 1.25 : 1}
                  strokeDasharray="4 3"
                  opacity={entry.isActive ? 0.6 : 0.4}
                />
                <polyline
                  points={line(entry.rows, outerHigh)}
                  fill="none"
                  stroke={entry.color}
                  strokeWidth={entry.isActive ? 1.25 : 1}
                  strokeDasharray="4 3"
                  opacity={entry.isActive ? 0.6 : 0.4}
                />
              </React.Fragment>
            ))}
            {orderedSeries.map((entry) => (
              <polyline
                key={`median-${entry.id}`}
                data-product-fan-series={entry.id}
                points={line(entry.rows, median)}
                fill="none"
                stroke={entry.color}
                strokeWidth={entry.isActive ? 2.75 : 2}
                opacity={entry.isActive ? 1 : 0.85}
              />
            ))}
          </>
        )}
        {selectedRows.length > 0 && (
          <>
            <polyline
              points={selectedLine}
              fill="none"
              stroke={selectedColor}
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="3"
              data-product-selected-rollout-line={selectedSeed}
            />
            <circle
              cx={x(selectedRows[selectedRows.length - 1])}
              cy={y(selectedRows[selectedRows.length - 1].value)}
              r="4"
              fill={selectedColor}
              stroke="white"
              strokeWidth="1.5"
            />
          </>
        )}
        {eventMarkers.map((markerProps) => (
          <FanEventMarker
            key={`${markerProps.event.kind}-${markerProps.event.monthIndex}-${markerProps.index}`}
            {...markerProps}
            x={x}
            y={y}
            top={margin.top}
            plotHeight={plotHeight}
            selectedEventMonthIndex={selectedEventMonthIndex}
            hoveredEventMonthIndex={hoveredEventMonthIndex}
            onSelectEventMonth={onSelectEventMonth}
            onHoverEventMonth={onHoverEventMonth}
          />
        ))}
        {hoveredRow &&
          (() => {
            // Single: the one scenario's percentiles. Multi: each scenario's median at this month.
            const tipLines = single
              ? sortedPercentiles.map((pct) => ({
                  key: `p${pct}`,
                  label: `P${pct}`,
                  color: null,
                  value: hoveredRow.values.get(pct),
                }))
              : series.map((entry) => ({
                  key: entry.id,
                  label: entry.label,
                  color: entry.color,
                  value: rowByMonthBySeries.get(entry.id)?.get(hoveredRow.monthIndex)?.values.get(median),
                }));
            const tipW = single ? 140 : 168;
            const tipH = 26 + tipLines.length * 14;
            const tipPad = 8;
            const tipX =
              x(hoveredRow) + tipPad + tipW <= svgWidth
                ? tipPad
                : x(hoveredRow) - tipPad - tipW >= 0
                  ? -tipPad - tipW
                  : Math.max(0, svgWidth - tipW - x(hoveredRow));
            const tipY = margin.top + plotHeight - tipH >= 0 ? 0 : margin.top + plotHeight - tipH;
            return (
              <>
                <line
                  x1={x(hoveredRow)}
                  y1={margin.top}
                  x2={x(hoveredRow)}
                  y2={margin.top + plotHeight}
                  stroke="rgba(100,116,139,0.5)"
                  strokeWidth="1"
                  strokeDasharray="3 2"
                />
                <g
                  transform={`translate(${x(hoveredRow) + tipX}, ${margin.top + tipY})`}
                  className="pointer-events-none"
                >
                  <rect
                    x={0}
                    y={0}
                    width={tipW}
                    height={tipH}
                    rx={4}
                    fill="white"
                    fillOpacity="0.92"
                    stroke="rgba(15,23,42,0.15)"
                    strokeWidth="1"
                  />
                  <text x={8} y={14} fontSize="10" fontWeight="600" fill="#334155">
                    Month {hoveredRow.monthIndex}
                    {single ? "" : " · median"}
                  </text>
                  {tipLines.map((tipLine, i) => (
                    <text key={tipLine.key} x={8} y={28 + i * 14} fontSize="10" fill="#64748b">
                      {tipLine.color != null && (
                        <tspan fill={tipLine.color} fontWeight="700">
                          ●{" "}
                        </tspan>
                      )}
                      {tipLine.label}:{" "}
                      {Number.isFinite(tipLine.value)
                        ? fmtMetricValue(metric.chartValue, tipLine.value, currencyDisplay)
                        : "n/a"}
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
