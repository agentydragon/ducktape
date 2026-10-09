import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Card, Group, SegmentedControl, Skeleton, Stack, Table, Text, Title } from "@mantine/core";
import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
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
  Tooltip
);

type Allowance = components["schemas"]["AllowanceView"];
type TransactionsView = components["schemas"]["SpendTransactionsView"];
type EstimatePeriodId = Extract<components["schemas"]["Period"]["id"], "rolling_7d" | "rolling_30d">;
type LegendRow = {
  key: string;
  label: string;
  value: string;
  color: string;
  marker: "square" | "dotted" | "dashed";
};

function legendColumnsForViewport(): number {
  if (typeof window === "undefined" || window.innerWidth >= 960) return 3;
  return window.innerWidth >= 600 ? 2 : 1;
}

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
  const [legendColumns, setLegendColumns] = useState(legendColumnsForViewport);
  const [legendOpen, setLegendOpen] = useState(() => legendColumnsForViewport() > 1);
  useEffect(() => {
    const updateColumns = () => setLegendColumns(legendColumnsForViewport());
    window.addEventListener("resize", updateColumns);
    return () => window.removeEventListener("resize", updateColumns);
  }, []);
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
    const dailyAverageMinorUnits = dates.length > 0 ? Math.round(totalMinorUnits / dates.length) : 0;
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
      // Give each reference line its own stack on the stacked y scale.
      {
        type: "line" as const,
        label: "Average over period",
        data: dates.map(() => dailyAverageMinorUnits),
        borderColor: "#1971C2",
        borderDash: [2, 3],
        borderWidth: 2,
        pointRadius: 0,
        pointHitRadius: 8,
        tension: 0,
        stack: "average",
        order: -2,
      },
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
        stack: "leash",
        order: -1,
      },
    ];
    return {
      labels: dayLabels,
      datasets,
      categories: orderedCategories.map(([id, category]) => ({ id, ...category })),
      dailyAverageMinorUnits,
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
          legend: { display: false },
          tooltip: {
            filter: (context) => {
              const value = Number(context.parsed.y ?? 0);
              return value > 0 && formatMoney(value, allowance.currency) !== formatMoney(0, allowance.currency);
            },
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
  const dailyAverageMinorUnits = chart?.dailyAverageMinorUnits ?? 0;
  const leashDailyMinorUnits = chart?.leashDailyMinorUnits ?? 0;
  const summary =
    chart && transactions
      ? `${formatMoney(totalMinorUnits, allowance.currency)} flexible spending over ${chart.days} days; average ${formatMoney(dailyAverageMinorUnits, allowance.currency)} per day; leash reference ${formatMoney(leashDailyMinorUnits, allowance.currency)} per day.`
      : "Spending history is loading.";
  const legendRows: LegendRow[] = chart
    ? [
        ...chart.categories.map((category) => ({
          key: category.id,
          label: category.label,
          value: formatMoney(category.total, allowance.currency),
          color: category.color,
          marker: "square" as const,
        })),
        {
          key: "average",
          label: "Average over period",
          value: `${formatMoney(dailyAverageMinorUnits, allowance.currency)}/day`,
          color: "#1971C2",
          marker: "dotted",
        },
        {
          key: "leash",
          label: "Leash level",
          value: `${formatMoney(leashDailyMinorUnits, allowance.currency)}/day`,
          color: "#E8590C",
          marker: "dashed",
        },
      ]
    : [];
  const legendRowsByColumn = Array.from({ length: Math.ceil(legendRows.length / legendColumns) }, (_, rowIndex) =>
    legendRows.slice(rowIndex * legendColumns, (rowIndex + 1) * legendColumns)
  );

  return (
    <Card component="section" aria-labelledby="spending-history-title" withBorder radius="lg" padding="xl">
      <Stack gap="md">
        <Group align="flex-start" justify="space-between" gap="sm" wrap="wrap">
          <div>
            <Title id="spending-history-title" order={2} size="h3">
              Spending over time
            </Title>
            <Text size="sm" c="dimmed" mt="xs">
              Daily flexible purchases by configured category. Days follow {allowance.time_zone ?? "UTC"}.
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
          <>
            <Text size="sm" c="dimmed" role="status" aria-live="polite">
              Updating spending history…
            </Text>
            <Skeleton height={250} radius="md" />
            <details
              open={legendOpen}
              onToggle={(event) => setLegendOpen(event.currentTarget.open)}
              style={{ minWidth: 0 }}
            >
              <summary style={{ cursor: "pointer", fontWeight: 600, marginBottom: legendOpen ? 8 : 0 }}>
                Category totals and reference lines
              </summary>
              <Table
                aria-label="Loading category totals and reference lines"
                verticalSpacing="xs"
                horizontalSpacing="sm"
                withTableBorder
              >
                <Table.Caption>
                  Loading totals for the selected period; reference lines show daily amounts.
                </Table.Caption>
                <Table.Thead>
                  <Table.Tr>
                    {Array.from({ length: legendColumns }, (_, columnIndex) => (
                      <Fragment key={`loading-legend-header-${columnIndex}`}>
                        <Table.Th scope="col">Series</Table.Th>
                        <Table.Th scope="col" ta="right">
                          Amount
                        </Table.Th>
                      </Fragment>
                    ))}
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {Array.from({ length: 4 }, (_, rowIndex) => (
                    <Table.Tr key={`loading-legend-row-${rowIndex}`}>
                      {Array.from({ length: legendColumns }, (_, columnIndex) => (
                        <Fragment key={`loading-legend-cell-${rowIndex}-${columnIndex}`}>
                          <Table.Td>
                            <Skeleton height={14} width="70%" />
                          </Table.Td>
                          <Table.Td>
                            <Skeleton height={14} width="60%" ml="auto" />
                          </Table.Td>
                        </Fragment>
                      ))}
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </details>
          </>
        ) : transactions && chart ? (
          <>
            <Text id="spending-history-summary" size="sm" c="dimmed">
              {summary}
            </Text>
            <div style={{ height: 250, minWidth: 0 }}>
              <canvas ref={canvasRef} role="img" aria-label={summary} aria-describedby="spending-history-summary" />
            </div>
            <details
              open={legendOpen}
              onToggle={(event) => setLegendOpen(event.currentTarget.open)}
              style={{ minWidth: 0 }}
            >
              <summary style={{ cursor: "pointer", fontWeight: 600, marginBottom: legendOpen ? 8 : 0 }}>
                Category totals and reference lines
              </summary>
              <Table
                aria-label={`Chart legend and totals for the selected ${chart.days}-day period`}
                verticalSpacing="xs"
                horizontalSpacing="sm"
                withTableBorder
              >
                <Table.Caption>
                  Category values total the selected {chart.days}-day period; reference lines show daily amounts.
                </Table.Caption>
                <Table.Thead>
                  <Table.Tr>
                    {Array.from({ length: legendColumns }, (_, columnIndex) => (
                      <Fragment key={`legend-header-${columnIndex}`}>
                        <Table.Th scope="col">Series</Table.Th>
                        <Table.Th scope="col" ta="right">
                          Amount
                        </Table.Th>
                      </Fragment>
                    ))}
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {legendRowsByColumn.map((rowGroup, rowIndex) => (
                    <Table.Tr key={`legend-row-${rowIndex}`}>
                      {rowGroup.map((row) => (
                        <Fragment key={row.key}>
                          <Table.Td>
                            <Group gap="xs" wrap="nowrap">
                              <span
                                aria-hidden="true"
                                style={
                                  row.marker === "square"
                                    ? {
                                        width: 12,
                                        height: 12,
                                        flex: "0 0 auto",
                                        backgroundColor: row.color,
                                      }
                                    : {
                                        width: 18,
                                        flex: "0 0 auto",
                                        borderTop: `2px ${row.marker} ${row.color}`,
                                      }
                                }
                              />
                              <span style={{ minWidth: 0, flex: "1 1 auto", overflowWrap: "anywhere" }}>
                                {row.label}
                              </span>
                            </Group>
                          </Table.Td>
                          <Table.Td ta="right" style={{ fontVariantNumeric: "tabular-nums" }}>
                            {row.value}
                          </Table.Td>
                        </Fragment>
                      ))}
                      {rowGroup.length < legendColumns && <Table.Td colSpan={2 * (legendColumns - rowGroup.length)} />}
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </details>
            <Text size="xs" c="dimmed">
              Posted and pending positive flexible purchases in the selected window are included, including purchases
              before allowance activation. Earlier purchases inform this graph and the pace estimate but do not reduce
              the available allowance. Unmatched purchases count as unclassified flexible spending; fixed and excluded
              purchases are omitted. The dotted average line shows average daily spending over this period; the dashed
              leash line shows monthly credit as a daily reference pace. Carryforward affects the available balance
              separately.
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
