import { fmtNumber, fmtPct, fmtUsd, fmtUsdCompact } from "./format";

const FAN_CHART_TICK_FRACTIONS = [0, 0.25, 0.5, 0.75, 1];
const LOG_SCALE_UNIT = 1;

export function normalizeMetricScale(metricScale) {
  return metricScale === "log" ? "log" : "linear";
}

export function metricIsCurrency(metricName) {
  return metricName?.endsWith("Usd") || metricName?.includes("Value") || metricName?.includes("CashFlow");
}

export function fmtMetricValue(metricName, value, currencyDisplay = "exact") {
  if (metricName?.endsWith("Pct")) {
    return fmtPct(value);
  }
  if (metricIsCurrency(metricName)) {
    return currencyDisplay === "compact" ? fmtUsdCompact(value) : fmtUsd(value);
  }
  return fmtNumber(value);
}

export function fmtAxisMetricValue(metricName, value) {
  return fmtMetricValue(metricName, value, "compact");
}

export function niceCurrencyTickStep(rawStep) {
  if (!Number.isFinite(rawStep) || rawStep <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const normalized = rawStep / magnitude;
  const niceNormalized = [1, 2, 2.5, 5, 10].find((candidate) => normalized <= candidate) ?? 10;
  return Math.max(1, niceNormalized * magnitude);
}

export function transformMetricValue(value, metricScale) {
  if (!Number.isFinite(value)) return NaN;
  if (normalizeMetricScale(metricScale) !== "log") return value;
  return Math.sign(value) * Math.log1p(Math.abs(value) / LOG_SCALE_UNIT);
}

export function axisCoordinate(axis, value) {
  return transformMetricValue(value, axis?.scale ?? "linear");
}

export function currencyFanChartAxis(values, targetTickCount = 5) {
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
  return { min: axisMin, max: axisMax, range: axisMax - axisMin, ticks, scale: "linear" };
}

function decadeTicksAscending(minPos, maxPos) {
  // Snap axis ticks to the closest 1·10^k / 3·10^k value that brackets the data, instead
  // of padding out to whole-decade boundaries. So data spanning $300K..$3M gets ticks
  // [$300K, $1M, $3M], not [$100K, $300K, $1M, $3M, $10M].
  const realSpan = Math.log10(maxPos / minPos);
  const multiples = realSpan > 5 ? [1] : [1, 3];
  const stepDecades = realSpan > 10 ? 2 : 1;
  const minDecade = Math.floor(Math.log10(minPos)) - 1;
  const maxDecade = Math.ceil(Math.log10(maxPos)) + 1;
  const candidates = [];
  for (let d = minDecade; d <= maxDecade; d += stepDecades) {
    for (const m of multiples) candidates.push(m * 10 ** d);
  }
  candidates.sort((left, right) => left - right);
  const lo = candidates.filter((value) => value <= minPos).pop() ?? candidates[0];
  const hi = candidates.find((value) => value >= maxPos) ?? candidates[candidates.length - 1];
  return candidates.filter((value) => value >= lo && value <= hi);
}

function crossZeroSideTicks(maxMag) {
  // Decade-only ticks for one side of a symlog axis. Skip 2/5 multiples because the other
  // side plus the explicit 0 tick would otherwise overload the axis with labels.
  const highDecade = Math.ceil(Math.log10(Math.max(maxMag, 1)));
  const lowDecade = Math.max(0, highDecade - 3);
  const ticks = [];
  for (let d = lowDecade; d <= highDecade; d += 1) ticks.push(10 ** d);
  return ticks;
}

function logFanChartAxis(values) {
  const scale = "log";
  if (values.length === 0) {
    const axisMax = transformMetricValue(10, scale);
    return { min: 0, max: axisMax, range: axisMax, ticks: [10, 1, 0], scale };
  }
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    const pad = Math.max(1, Math.abs(min) * 9);
    min -= pad;
    max += pad;
  }
  const tickValues = new Set<number>();
  if (min > 0) {
    for (const value of decadeTicksAscending(Math.max(min, 1), Math.max(max, 1))) tickValues.add(value);
  } else if (max < 0) {
    for (const value of decadeTicksAscending(Math.max(-max, 1), Math.max(-min, 1))) tickValues.add(-value);
  } else {
    if (max > 0) for (const value of crossZeroSideTicks(max)) tickValues.add(value);
    if (min < 0) for (const value of crossZeroSideTicks(-min)) tickValues.add(-value);
    tickValues.add(0);
  }
  const sorted = Array.from(tickValues).sort((left, right) => left - right);
  const axisMin = transformMetricValue(sorted[0], scale);
  const axisMax = transformMetricValue(sorted[sorted.length - 1], scale);
  const range = axisMax - axisMin || 1;
  return { min: axisMin, max: axisMax, range, ticks: sorted.slice().reverse(), scale };
}

function linearTransformedFanChartAxis(values) {
  const scale = "linear";
  if (values.length === 0) {
    return {
      min: 0,
      max: 1,
      range: 1,
      ticks: FAN_CHART_TICK_FRACTIONS.map((tick) => 1 - tick),
      scale,
    };
  }
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    const pad = Math.max(1, Math.abs(max) * 0.15);
    min -= pad;
    max += pad;
  }
  const range = max - min;
  return {
    min,
    max,
    range,
    ticks: FAN_CHART_TICK_FRACTIONS.map((tick) => min + range * (1 - tick)),
    scale,
  };
}

export function fanChartAxis(metricName, values, metricScale = "linear") {
  const finiteValues = values.filter(Number.isFinite);
  if (normalizeMetricScale(metricScale) === "log") {
    return logFanChartAxis(finiteValues);
  }
  if (finiteValues.length === 0) {
    return linearTransformedFanChartAxis([]);
  }
  if (metricIsCurrency(metricName)) {
    return currencyFanChartAxis(finiteValues);
  }
  return linearTransformedFanChartAxis(finiteValues);
}

export function fanChartYearTicks(maxYear) {
  const maxWholeYear = Math.max(1, Math.ceil(maxYear));
  const step = Math.max(1, Math.ceil(maxWholeYear / 5));
  const ticks = [];
  for (let year = 0; year <= maxWholeYear; year += step) {
    ticks.push(year);
  }
  if (ticks[ticks.length - 1] !== maxWholeYear) {
    ticks.push(maxWholeYear);
  }
  return ticks;
}
