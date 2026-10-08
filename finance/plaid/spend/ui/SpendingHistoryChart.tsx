import { useEffect, useMemo, useRef } from "react";
import { Alert, Card, Center, Group, Loader, SegmentedControl, Stack, Text, Title } from "@mantine/core";
import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
} from "chart.js";
import type { components } from "./api/schema";

Chart.register(
  BarController,
  BarElement,
  CategoryScale,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
  Legend
);

type Allowance = components["schemas"]["AllowanceView"];
type TransactionsView = components["schemas"]["SpendTransactionsView"];
type EstimatePeriodId = Extract<components["schemas"]["Period"]["id"], "rolling_7d" | "rolling_30d">;

function formatMoney(minorUnits: number, currency: string): string {
  const code = currency.length === 3 ? currency.toUpperCase() : "USD";
  const fractionDigits = new Intl.NumberFormat(undefined, { style: "currency", currency: code }).resolvedOptions()
    .maximumFractionDigits;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: code,
    maximumFractionDigits: 0,
  }).format(minorUnits / 10 ** (fractionDigits ?? 2));
}

function datesInPeriod(start: string, end: string): string[] {
  const startTime = Date.parse(`${start}T00:00:00Z`);
  const endTime = Date.parse(`${end}T00:00:00Z`);
  const dates: string[] = [];
  for (let timestamp = startTime; timestamp <= endTime; timestamp += 86_400_000) {
    dates.push(new Date(timestamp).toISOString().slice(0, 10));
  }
  return dates;
}

export function SpendingHistoryChart({
  transactions,
  allowance,
  loading,
  error,
  periodId,
  onPeriodChange,
}: {
  transactions: TransactionsView | null;
  allowance: Allowance;
  loading: boolean;
  error: string | null;
  periodId: EstimatePeriodId;
  onPeriodChange: (periodId: EstimatePeriodId) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dates = useMemo(
    () => (transactions ? datesInPeriod(transactions.period.start, transactions.period.end) : []),
    [transactions]
  );
  const chart = useMemo(() => {
    if (!transactions) return null;
    const amounts = new Map<string, Map<string, number>>();
    const categories = new Map<string, { label: string; color: string; total: number }>();
    for (const row of transactions.rows) {
      const amount = row.pace_effects.find((effect) => effect.period_id === periodId)?.amount_minor_units ?? 0;
      if (amount <= 0 || !row.category) continue;
      const category = categories.get(row.category.id) ?? {
        label: row.category.label,
        color: row.category.color,
        total: 0,
      };
      category.total += amount;
      categories.set(row.category.id, category);
      const day = amounts.get(row.date) ?? new Map<string, number>();
      day.set(row.category.id, (day.get(row.category.id) ?? 0) + amount);
      amounts.set(row.date, day);
    }

    const dayLabels = dates.map((date) =>
      new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" }).format(
        new Date(`${date}T00:00:00Z`)
      )
    );
    const orderedCategories = [...categories.entries()].sort((left, right) => right[1].total - left[1].total);
    const leashDailyMinorUnits = Math.round((allowance.monthly_minor_units * 12) / 365.2425);
    const totalMinorUnits = orderedCategories.reduce((total, [, category]) => total + category.total, 0);
    const datasets = [
      ...orderedCategories.map(([id, category]) => ({
        type: "bar" as const,
        label: category.label,
        data: dates.map((date) => amounts.get(date)?.get(id) ?? 0),
        backgroundColor: category.color,
        borderColor: category.color,
        borderWidth: 0,
        stack: "spending",
      })),
      {
        type: "line" as const,
        label: "Leash level",
        data: dates.map(() => leashDailyMinorUnits),
        borderColor: "#E8590C",
        borderDash: [6, 4],
        borderWidth: 2,
        pointRadius: 0,
        pointHitRadius: 8,
        tension: 0,
        order: -1,
      },
    ];
    return {
      labels: dayLabels,
      datasets,
      leashDailyMinorUnits,
      totalMinorUnits,
      days: dates.length,
    };
  }, [allowance.monthly_minor_units, dates, periodId, transactions]);

  useEffect(() => {
    if (!canvasRef.current || !transactions || !chart) return;
    const instance = new Chart(canvasRef.current, {
      type: "bar",
      data: {
        labels: chart.labels,
        datasets: chart.datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom" },
          tooltip: {
            callbacks: {
              label: (context) =>
                `${context.dataset.label}: ${formatMoney(Number(context.parsed.y ?? 0), allowance.currency)}`,
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            title: { display: true, text: "Flexible spending per day" },
            ticks: {
              callback: (value) => formatMoney(Number(value), allowance.currency),
            },
          },
        },
        animation: false,
      },
    });
    return () => instance.destroy();
  }, [allowance.currency, chart, transactions]);

  const totalMinorUnits = chart?.totalMinorUnits ?? 0;
  const leashDailyMinorUnits = chart?.leashDailyMinorUnits ?? 0;
  const dailyAverageMinorUnits = chart?.days ? Math.round(totalMinorUnits / chart.days) : 0;
  const summary =
    chart && transactions
      ? `${formatMoney(totalMinorUnits, allowance.currency)} flexible spending over ${chart.days} days; average ${formatMoney(dailyAverageMinorUnits, allowance.currency)} per day; leash reference ${formatMoney(leashDailyMinorUnits, allowance.currency)} per day.`
      : "Spending history is loading.";

  return (
    <Card component="section" aria-labelledby="spending-history-title" withBorder radius="lg" padding="xl">
      <Stack gap="md">
        <Group align="flex-start" justify="space-between" gap="sm" wrap="wrap">
          <div>
            <Title id="spending-history-title" order={2} size="h3">
              Spending over time
            </Title>
            <Text size="sm" c="dimmed" mt="xs">
              Daily flexible purchases by configured category.
            </Text>
          </div>
          <Stack gap={4} align="flex-end" style={{ flexShrink: 0 }}>
            <Text size="xs" c="dimmed" fw={600}>
              Estimate window
            </Text>
            <SegmentedControl
              aria-label="Estimate window"
              size="xs"
              value={periodId}
              onChange={(value) => onPeriodChange(value as EstimatePeriodId)}
              data={[
                { value: "rolling_7d", label: "7 days" },
                { value: "rolling_30d", label: "30 days" },
              ]}
            />
          </Stack>
        </Group>
        {error ? (
          <Alert color="red" title="Couldn't load spending history">
            {error}
          </Alert>
        ) : loading && !transactions ? (
          <Center mih={220}>
            <Loader size="sm" />
          </Center>
        ) : transactions && chart ? (
          <>
            <Text id="spending-history-summary" size="sm" c="dimmed">
              {summary}
            </Text>
            <div style={{ height: 250, minWidth: 0 }}>
              <canvas ref={canvasRef} role="img" aria-label={summary} aria-describedby="spending-history-summary" />
            </div>
            <Text size="xs" c="dimmed">
              Posted and pending positive flexible purchases in the selected window are included, including purchases
              before allowance activation. Earlier purchases inform this graph and the pace estimate but do not reduce
              the available allowance. Unmatched purchases count as unclassified flexible spending; fixed and excluded
              purchases are omitted. The dashed leash line shows the monthly credit as a daily reference pace;
              carryforward affects the available balance separately.
            </Text>
          </>
        ) : (
          <Text size="sm" c="dimmed">
            No spending history is available for this period.
          </Text>
        )}
      </Stack>
    </Card>
  );
}
