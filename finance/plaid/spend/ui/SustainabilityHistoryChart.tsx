import { useEffect, useMemo, useRef, useState } from "react";
import { Checkbox, Group, Paper, Stack, Switch, Text, Title } from "@mantine/core";
import { CategoryScale, Chart, LinearScale, LineController, LineElement, PointElement, Tooltip } from "chart.js";
import Decimal from "decimal.js";

import type { SustainabilityView } from "./schema";
import { annualFundingMinorUnits } from "./decimal_math";
import { money } from "./spend_display";
import type { SustainabilityHistory } from "./types";

const WINDOW_COLORS: Record<number, string> = {
  7: "#6741D9",
  30: "#1971C2",
  90: "#0C8599",
  180: "#2F9E44",
  365: "#E67700",
  730: "#D6336C",
};
const REFERENCE_SCENARIO_COLOR = "#868E96";

Chart.register(CategoryScale, LinearScale, LineController, LineElement, PointElement, Tooltip);

type HistoryWindow = {
  view: SustainabilityView;
  color: string;
  pointsByDate: Map<string, SustainabilityHistory>;
};

type HistoryTooltip = {
  endpoint: string;
  left: number;
  top: number;
  windows: { label: string; color: string; lines: string[] }[];
  references: string[];
};

function windowColor(windowDays: number): string {
  return WINDOW_COLORS[windowDays] ?? `hsl(${((windowDays * 137.508) % 360).toFixed(1)} 58% 42%)`;
}

function percent(value: string | number): string {
  const formatter = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 });
  if (typeof value === "number") return formatter.format(value);
  const roundedRate = new Decimal(value).mul(100).toDecimalPlaces(1, Decimal.ROUND_HALF_UP).div(100);
  return formatter.format(roundedRate.toNumber());
}

function shortDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" }).format(
    new Date(`${value}T00:00:00Z`)
  );
}

function fullDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${value}T00:00:00Z`)
  );
}

function inclusiveWindowRange(endDate: string, windowDays: number): string {
  const end = new Date(`${endDate}T00:00:00Z`);
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - (windowDays - 1));
  const formatter = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
  return `${formatter.format(start)} – ${formatter.format(end)}`;
}

function missingWindowMessage(view: SustainabilityView, hasRates: boolean): string | null {
  if (view.history.length === 0) {
    return `History requires at least ${view.window_days} consecutive days of covered spending history. No complete ${view.window_days}-day windows are available in the reported history yet.`;
  }
  if (!view.history.some((point) => point.annualized_spending_minor_units != null)) {
    return `${view.window_days}-day spending history is unavailable; review account and source coverage in spending details.`;
  }
  if (hasRates && !view.history.some((point) => point.tax_adjusted_draw_rate != null)) {
    return `Spending history exists for the ${view.window_days}-day window, but its tax-inclusive withdrawal rates are unavailable.`;
  }
  return null;
}

function tooltipLines(
  window: HistoryWindow,
  endpoint: string,
  hasRates: boolean,
  currency: string,
  portfolioMinorUnits: number | null
): string[] {
  const lines = [`${window.view.window_days}-day window · ${inclusiveWindowRange(endpoint, window.view.window_days)}`];
  const point = window.pointsByDate.get(endpoint);
  if (!point) {
    lines.push("No history point is available at this endpoint.");
    return lines;
  }
  if (point.status === "provisional") {
    lines.push("Provisional point: a reversal is unverified, so ordinary accounting is retained.");
  } else if (point.status === "unavailable") {
    lines.push("This history point is unavailable.");
  }

  const spending = point.annualized_spending_minor_units;
  lines.push(`Annual spending excluding taxes: ${spending == null ? "unavailable" : money(spending, currency)}`);
  if (hasRates && portfolioMinorUnits != null) {
    const rate = point.tax_adjusted_draw_rate;
    if (rate == null || spending == null) {
      lines.push("Estimated withdrawal taxes: unavailable · Annual funding including taxes: unavailable");
      lines.push(
        `Required withdrawal rate including taxes: unavailable · Based on ${point.covered_days} complete days`
      );
    } else {
      const funding = annualFundingMinorUnits(portfolioMinorUnits, rate);
      lines.push(
        `Estimated withdrawal taxes: ${money(funding.minus(spending), currency)} · Annual funding including taxes: ${money(funding, currency)}`
      );
      lines.push(
        `Required withdrawal rate including taxes: ${percent(rate)} · Based on ${point.covered_days} complete days`
      );
    }
  } else {
    lines.push(`Based on ${point.covered_days} complete calendar days`);
  }
  return lines;
}

export function SustainabilityHistoryChart({
  windows,
  selectedWindowDays,
  referenceDrawRates,
  currency,
  portfolioMinorUnits,
  effectiveTaxRate,
}: {
  windows: SustainabilityView[];
  selectedWindowDays: number;
  referenceDrawRates: string[];
  currency: string;
  portfolioMinorUnits: number | null;
  effectiveTaxRate: string | null;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [includeTaxes, setIncludeTaxes] = useState(true);
  const [hiddenWindowDays, setHiddenWindowDays] = useState<Set<number>>(() => new Set());
  const [tooltip, setTooltip] = useState<HistoryTooltip | null>(null);
  const hasRates = effectiveTaxRate != null && portfolioMinorUnits != null && portfolioMinorUnits > 0;
  const amountLabel = includeTaxes ? "Annual funding including taxes" : "Annual spending excluding taxes";

  const historyWindows = useMemo(
    () =>
      [...windows]
        .sort((left, right) => left.window_days - right.window_days)
        .map((view) => ({
          view,
          color: windowColor(view.window_days),
          pointsByDate: new Map(view.history.map((point) => [point.date, point] as const)),
        })),
    [windows]
  );
  const visibleWindows = useMemo(
    () => historyWindows.filter(({ view }) => !hiddenWindowDays.has(view.window_days)),
    [historyWindows, hiddenWindowDays]
  );
  const dates = useMemo(
    () => [...new Set(windows.flatMap((view) => view.history.map((point) => point.date)))].sort(),
    [windows]
  );
  const chart = useMemo(() => {
    const curveValues = visibleWindows.map(({ pointsByDate }) =>
      dates.map((date) => {
        const point = pointsByDate.get(date);
        if (!hasRates) return point?.annualized_spending_minor_units ?? null;
        const rate = point?.tax_adjusted_draw_rate;
        return rate == null ? null : new Decimal(rate).toNumber();
      })
    );
    const fittedValues = [
      ...curveValues.flatMap((values) =>
        values.filter((value): value is number => value != null && Number.isFinite(value))
      ),
      ...(hasRates ? referenceDrawRates.map((rate) => new Decimal(rate).toNumber()).filter(Number.isFinite) : []),
    ];
    const minimum = fittedValues.length > 0 ? Math.min(...fittedValues) : 0;
    const maximum = fittedValues.length > 0 ? Math.max(...fittedValues) : 1;
    const padding = (maximum - minimum || Math.abs(maximum) || 1) * 0.05;
    const datasets = [
      ...visibleWindows.map(({ view, color }, index) => {
        const values = curveValues[index];
        return {
          type: "line" as const,
          label: `${view.window_days}-day trailing average`,
          data: values,
          borderColor: color,
          backgroundColor: color,
          borderWidth: view.window_days === selectedWindowDays ? 3 : 2,
          pointRadius: values.map((value, pointIndex) =>
            value != null && values[pointIndex - 1] == null && values[pointIndex + 1] == null ? 3 : 0
          ),
          pointHitRadius: 8,
          spanGaps: false,
          tension: 0,
          yAxisID: "amount",
        };
      }),
      ...(hasRates
        ? referenceDrawRates.map((rate, index) => ({
            type: "line" as const,
            label: `Reference scenario ${index + 1} (${percent(rate)})`,
            data: dates.map(() => new Decimal(rate).toNumber()),
            borderColor: REFERENCE_SCENARIO_COLOR,
            backgroundColor: REFERENCE_SCENARIO_COLOR,
            borderWidth: 1,
            borderDash: [2, 4],
            pointRadius: 0,
            pointHitRadius: 4,
            yAxisID: "amount",
          }))
        : []),
    ];
    return {
      bounds: { min: minimum - padding, max: maximum + padding },
      labels: dates.map(shortDate),
      datasets,
      curveValues,
    };
  }, [dates, hasRates, referenceDrawRates, selectedWindowDays, visibleWindows]);

  const hasVisibleCurveData = chart.curveValues.some((values) =>
    values.some((value) => value != null && Number.isFinite(value))
  );
  const canPlot = visibleWindows.length > 0 && hasVisibleCurveData && (!includeTaxes || hasRates);
  const missingMessages = historyWindows
    .map(({ view }) => ({ view, message: missingWindowMessage(view, hasRates) }))
    .filter((item): item is { view: SustainabilityView; message: string } => item.message != null);

  useEffect(() => {
    if (!canvasRef.current || !canPlot) return;
    const amountPerRate = hasRates
      ? new Decimal(portfolioMinorUnits).mul(includeTaxes ? 1 : new Decimal(1).minus(effectiveTaxRate ?? 0))
      : new Decimal(1);
    const instance = new Chart(canvasRef.current, {
      type: "line",
      data: { labels: chart.labels, datasets: chart.datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: false,
            external: ({ chart: chartInstance, tooltip: chartTooltip }) => {
              if (chartTooltip.opacity === 0 || chartTooltip.dataPoints.length === 0) {
                setTooltip(null);
                return;
              }

              const endpoint = dates[chartTooltip.dataPoints[0].dataIndex];
              const parent = chartInstance.canvas.parentElement;
              if (!endpoint || !parent) return;

              const parentBounds = parent.getBoundingClientRect();
              const canvasBounds = chartInstance.canvas.getBoundingClientRect();
              const tooltipWidth = Math.min(Math.max(0, parent.clientWidth - 16), 360);
              const estimatedHeight = Math.min(
                parent.clientHeight - 16,
                48 + visibleWindows.length * 112 + (hasRates ? referenceDrawRates.length * 20 : 0)
              );
              const pointerX = chartTooltip.caretX + canvasBounds.left - parentBounds.left;
              const pointerY = chartTooltip.caretY + canvasBounds.top - parentBounds.top;
              const left = Math.max(8, Math.min(parent.clientWidth - tooltipWidth - 8, pointerX - tooltipWidth / 2));
              const top = Math.max(
                8,
                Math.min(parent.clientHeight - estimatedHeight - 8, pointerY - estimatedHeight / 2)
              );
              setTooltip({
                endpoint,
                left,
                top,
                windows: visibleWindows.map((window) => ({
                  label: `${window.view.window_days}-day trailing average`,
                  color: window.color,
                  lines: tooltipLines(window, endpoint, hasRates, currency, portfolioMinorUnits),
                })),
                references: hasRates
                  ? referenceDrawRates.map((rate, index) => `Reference scenario ${index + 1}: ${percent(rate)}`)
                  : [],
              });
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 9 } },
          amount: {
            type: "linear",
            position: "left",
            ...chart.bounds,
            title: { display: true, text: amountLabel },
            ticks: {
              count: 6,
              autoSkip: false,
              callback: (value) => money(new Decimal(value).mul(amountPerRate), currency),
            },
          },
          rate: {
            type: "linear",
            position: "right",
            display: hasRates,
            ...chart.bounds,
            title: { display: true, text: "Required withdrawal rate including taxes" },
            grid: { drawOnChartArea: false },
            ticks: { count: 6, autoSkip: false, callback: (value) => percent(Number(value)) },
          },
        },
        animation: false,
      },
    });
    return () => {
      instance.destroy();
      setTooltip(null);
    };
  }, [
    amountLabel,
    canPlot,
    chart,
    currency,
    dates,
    effectiveTaxRate,
    hasRates,
    includeTaxes,
    portfolioMinorUnits,
    referenceDrawRates,
    visibleWindows,
  ]);

  return (
    <Paper component="section" aria-labelledby="sustainability-history-title" withBorder radius="lg" p="lg">
      <Stack gap="sm">
        <div>
          <Title id="sustainability-history-title" order={2} size="h4">
            Spending and funding history
          </Title>
          <Text size="sm" mt={4}>
            Configured trailing averages, annualized
          </Text>
          <Text size="xs" c="dimmed">
            Each line needs a complete trailing window. Gaps indicate unavailable account history; earlier complete
            windows remain visible.
          </Text>
          <Text size="xs" c="dimmed">
            Withdrawal percentages include taxes and use TODAY&apos;S portfolio valuation for every date shown.
          </Text>
        </div>
        <Group role="group" aria-label="Visible history windows" gap="md" wrap="wrap">
          {historyWindows.map(({ view, color }) => {
            const visible = !hiddenWindowDays.has(view.window_days);
            const selectedAndVisible = view.window_days === selectedWindowDays && visible;
            return (
              <Checkbox
                key={view.window_days}
                checked={visible}
                aria-label={`Show ${view.window_days}-day average`}
                onChange={(event) => {
                  const checked = event.currentTarget.checked;
                  setHiddenWindowDays((current) => {
                    const next = new Set(current);
                    if (checked) next.delete(view.window_days);
                    else next.add(view.window_days);
                    return next;
                  });
                }}
                label={
                  <Group gap={6} wrap="nowrap">
                    <span
                      aria-hidden="true"
                      style={{ borderTop: `2px solid ${color}`, display: "inline-block", width: 20 }}
                    />
                    <Text size="sm" fw={selectedAndVisible ? 700 : undefined}>
                      {view.window_days}-day trailing average{selectedAndVisible ? " · selected" : ""}
                    </Text>
                  </Group>
                }
              />
            );
          })}
        </Group>
        <Text size="xs" fw={600}>
          {amountLabel}
        </Text>
        <Text size="xs" c="dimmed">
          These checkboxes control chart visibility only; they do not change the selected sustainability detail window.
        </Text>
        <Switch
          label="Include withdrawal taxes in dollar amounts"
          checked={includeTaxes}
          onChange={(event) => setIncludeTaxes(event.currentTarget.checked)}
        />
        <Text size="xs" c="dimmed">
          {effectiveTaxRate == null
            ? "Withdrawal taxes are not modeled; funding amounts and withdrawal percentages are unavailable. Turn off withdrawal taxes in dollar amounts to view spending."
            : `Modeled withdrawal tax: ${percent(effectiveTaxRate)} of gross funding. Switching dollar amounts keeps the same tax-inclusive withdrawal percentage.`}
        </Text>
        {hasRates && referenceDrawRates.length > 0 && (
          <Group gap="xs" wrap="wrap" role="list" aria-label="Reference scenario legend">
            <Group gap={6} wrap="nowrap" role="listitem">
              <span
                aria-hidden="true"
                style={{ borderTop: `2px dotted ${REFERENCE_SCENARIO_COLOR}`, display: "inline-block", width: 20 }}
              />
              <Text size="xs">Reference withdrawal scenarios</Text>
            </Group>
          </Group>
        )}
        {missingMessages.length > 0 && (
          <Stack gap={2} aria-label="History availability by window">
            {missingMessages.map(({ view, message }) => (
              <Text key={view.window_days} size="sm" c="dimmed">
                {message}
              </Text>
            ))}
          </Stack>
        )}
        {historyWindows.length === 0 && (
          <Text size="sm" c="dimmed">
            No history windows are configured.
          </Text>
        )}
        {visibleWindows.length === 0 ? (
          <Text size="sm" c="dimmed">
            Select at least one history window to show its curve.
          </Text>
        ) : includeTaxes && !hasRates ? (
          <Text size="sm" c="dimmed">
            Turn off withdrawal taxes in dollar amounts to view spending excluding taxes.
          </Text>
        ) : !hasVisibleCurveData ? (
          <Text size="sm" c="dimmed">
            No data is available for the visible history windows.
          </Text>
        ) : (
          <div style={{ height: Math.max(320, 230 + historyWindows.length * 70), minWidth: 0, position: "relative" }}>
            <canvas ref={canvasRef} aria-label={`${amountLabel} by day`} role="img" />
            {tooltip && (
              <div
                role="tooltip"
                aria-label={`History ending ${fullDate(tooltip.endpoint)}`}
                style={{
                  position: "absolute",
                  left: tooltip.left,
                  top: tooltip.top,
                  width: 360,
                  maxWidth: "calc(100% - 16px)",
                  padding: 10,
                  border: "1px solid var(--mantine-color-default-border)",
                  borderRadius: 8,
                  background: "var(--mantine-color-body)",
                  boxShadow: "var(--mantine-shadow-md)",
                  pointerEvents: "none",
                  zIndex: 2,
                }}
              >
                <Text size="xs" fw={700} mb={6}>
                  {fullDate(tooltip.endpoint)}
                </Text>
                <Stack gap="xs">
                  {tooltip.windows.map((window) => (
                    <div key={window.label}>
                      <Group gap={6} wrap="nowrap">
                        <span
                          aria-hidden="true"
                          style={{ borderTop: `2px solid ${window.color}`, display: "inline-block", width: 16 }}
                        />
                        <Text size="xs" fw={600}>
                          {window.label}
                        </Text>
                      </Group>
                      <Stack gap={0} ml={22}>
                        {window.lines.map((line) => (
                          <Text key={line} size="xs">
                            {line}
                          </Text>
                        ))}
                      </Stack>
                    </div>
                  ))}
                  {tooltip.references.map((reference) => (
                    <Text key={reference} size="xs" c="dimmed">
                      {reference}
                    </Text>
                  ))}
                </Stack>
              </div>
            )}
          </div>
        )}
      </Stack>
    </Paper>
  );
}
