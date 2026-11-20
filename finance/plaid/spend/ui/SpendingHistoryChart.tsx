import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Group,
  SegmentedControl,
  Skeleton,
  Stack,
  Text,
  Title,
  useComputedColorScheme,
} from "@mantine/core";
import { chartPalette, mountChart, movingAverageColor } from "./chart";
import type { AllowanceView, PaceHistory } from "./schema";
import { HelpTooltip } from "../../../../util/frontend/help_tooltip";
import { money } from "./spend_display";

export function SpendingHistoryChart({ allowance, revision }: { allowance: AllowanceView; revision: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [days, setDays] = useState("30");
  const [history, setHistory] = useState<PaceHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [show7, setShow7] = useState(true);
  const [show30, setShow30] = useState(true);
  const [showBars, setShowBars] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [hovered, setHovered] = useState<string | null>(null);
  const highlighted = useMemo(() => new Set([...selected, ...(hovered ? [hovered] : [])]), [selected, hovered]);
  const scheme = useComputedColorScheme("light");
  const average7Color = movingAverageColor(scheme, 7);
  const average30Color = movingAverageColor(scheme, 30);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setHistory(null);
    const load = async () => {
      try {
        const response = await fetch(`/api/v1/spending-history?history_days=${days}`, {
          credentials: "same-origin",
          cache: "no-store",
          signal: controller.signal,
        });
        if (response.status === 401) {
          window.location.assign("/auth/login");
          return;
        }
        if (!response.ok) throw new Error(`History request returned ${response.status}`);
        const value: PaceHistory | null = await response.json();
        if (!controller.signal.aborted) setHistory(value);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load history");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void load();
    return () => controller.abort();
  }, [days, revision]);

  const chart = useMemo(() => {
    if (!history || history.status !== "active") return null;
    const totals = new Map<string, number>();
    for (const day of history.daily) {
      for (const [id, amount] of Object.entries(day.categories)) totals.set(id, (totals.get(id) ?? 0) + amount);
    }
    const categories = [...totals.entries()]
      .sort((a, b) => b[1] - a[1])
      .flatMap(([id, total]) => {
        const category = history.categories[id];
        return category ? [{ id, total, ...category }] : [];
      });
    const labels = history.daily.map((day) =>
      new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" }).format(
        new Date(`${day.date}T00:00:00Z`)
      )
    );
    const datasets = [
      ...(showBars ? categories : []).map((category) => ({
        type: "bar" as const,
        label: category.label,
        data: history.daily.map((day) => day.categories[category.id] ?? 0),
        backgroundColor: highlighted.size > 0 && !highlighted.has(category.id) ? `${category.color}55` : category.color,
        borderColor: highlighted.has(category.id) ? chartPalette[scheme].emphasis : category.color,
        borderWidth: highlighted.has(category.id) ? 2 : 0,
        borderSkipped: false as const,
        stack: "spending",
      })),
      ...history.windows
        .filter(
          (window) => (window.period_id === "rolling_7d" && show7) || (window.period_id === "rolling_30d" && show30)
        )
        .map((window) => ({
          type: "line" as const,
          label: window.period_id === "rolling_7d" ? "7-day moving average" : "30-day moving average",
          data: window.points.map((point) => point.observed_daily_minor_units),
          borderColor: window.period_id === "rolling_7d" ? average7Color : average30Color,
          borderWidth: 2,
          pointRadius: 0,
          pointHitRadius: 8,
          spanGaps: false,
          stack: window.period_id,
          order: -2,
        })),
      {
        type: "line" as const,
        label: "Leash level",
        data: history.daily.map(() => history.daily_reference_minor_units),
        borderColor: chartPalette[scheme].leash,
        borderDash: [6, 4],
        borderWidth: 2,
        pointRadius: 0,
        pointHitRadius: 8,
        stack: "leash",
        order: -1,
      },
    ];
    return { labels, datasets, categories, total: categories.reduce((sum, category) => sum + category.total, 0) };
  }, [history, highlighted, show7, show30, showBars, scheme, average7Color, average30Color]);

  useEffect(() => {
    if (!canvasRef.current || !chart) return;
    return mountChart(canvasRef.current, {
      type: "bar",
      data: { labels: chart.labels, datasets: chart.datasets },
      options: {
        plugins: {
          tooltip: {
            filter: (context) => Number(context.parsed.y ?? 0) > 0,
            callbacks: {
              label: (context) =>
                `${context.dataset.label}: ${money(Number(context.parsed.y ?? 0), allowance.currency)}`,
            },
          },
        },
        scales: {
          x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } },
          y: {
            stacked: true,
            beginAtZero: true,
            title: { display: true, text: "Flexible spending per day" },
            ticks: { callback: (value) => money(Number(value), allowance.currency) },
          },
        },
      },
    });
  }, [allowance.currency, chart]);

  const incomplete = history?.daily.some((day) => !day.coverage_complete) ?? false;
  const summary = chart
    ? `${money(chart.total, allowance.currency)} recorded flexible spending over ${days} days${incomplete ? "; account history is incomplete for some days" : ""}.`
    : "Spending history is loading.";
  return (
    <Card component="section" aria-labelledby="spending-history-title" withBorder radius="lg" padding="xl">
      <Stack gap="md">
        <Group justify="space-between" align="flex-start">
          <div>
            <Title id="spending-history-title" order={2} size="h3">
              Spending over time
            </Title>
            <Text size="xs" c="dimmed">
              Daily flexible purchases · {allowance.time_zone}
            </Text>
          </div>
          <Stack gap={4}>
            <Group gap="xs">
              <Text size="xs" fw={600}>
                History
              </Text>
              <HelpTooltip
                topic="History"
                description="Choose the number of calendar days shown. Moving-average windows are controlled separately."
              />
            </Group>
            <SegmentedControl
              aria-label="History days"
              size="xs"
              value={days}
              onChange={setDays}
              data={[
                { value: "7", label: "7 days" },
                { value: "30", label: "30 days" },
                { value: "90", label: "90 days" },
                { value: "365", label: "365 days" },
              ]}
            />
          </Stack>
        </Group>
        <Group gap="md">
          <Checkbox
            label="Daily bars"
            checked={showBars}
            onChange={(event) => setShowBars(event.currentTarget.checked)}
          />
          <Checkbox
            label={
              <>
                <span
                  aria-hidden="true"
                  style={{
                    display: "inline-block",
                    width: 16,
                    borderTop: `3px solid ${average7Color}`,
                    marginRight: 6,
                    verticalAlign: "middle",
                  }}
                />
                7-day moving average
              </>
            }
            checked={show7}
            onChange={(event) => setShow7(event.currentTarget.checked)}
          />
          <Checkbox
            label={
              <>
                <span
                  aria-hidden="true"
                  style={{
                    display: "inline-block",
                    width: 16,
                    borderTop: `3px solid ${average30Color}`,
                    marginRight: 6,
                    verticalAlign: "middle",
                  }}
                />
                30-day moving average
              </>
            }
            checked={show30}
            onChange={(event) => setShow30(event.currentTarget.checked)}
          />
        </Group>
        {error ? (
          <Alert color="red" title="Couldn't load spending history">
            {error}
          </Alert>
        ) : loading ? (
          <Skeleton height={250} radius="md" />
        ) : chart && history ? (
          <>
            <Text id="spending-history-summary" size="sm" c="dimmed">
              {summary}
            </Text>
            {incomplete && (
              <Alert color="yellow" title="Incomplete history">
                Some days lack complete account coverage. Bars show recorded purchases; moving averages have gaps where
                coverage is unknown.
              </Alert>
            )}
            <div style={{ height: 250, minWidth: 0 }}>
              <canvas
                ref={canvasRef}
                role="img"
                aria-label={summary}
                aria-describedby="spending-history-summary"
                data-highlighted-categories={[...highlighted].sort().join(",")}
              />
            </div>
            <details open>
              <summary>Category totals and reference lines</summary>
              <Group gap="xs" mt="xs">
                {chart.categories.map((category) => (
                  <Button
                    key={category.id}
                    size="xs"
                    styles={{
                      root: { maxWidth: "100%", height: "auto", minHeight: 30 },
                      label: { whiteSpace: "normal" },
                    }}
                    variant={selected.has(category.id) ? "filled" : "light"}
                    aria-pressed={selected.has(category.id)}
                    onClick={() =>
                      setSelected((current) => {
                        const next = new Set(current);
                        if (next.has(category.id)) next.delete(category.id);
                        else next.add(category.id);
                        return next;
                      })
                    }
                    onMouseEnter={() => setHovered(category.id)}
                    onMouseLeave={() => setHovered(null)}
                    onFocus={() => setHovered(category.id)}
                    onBlur={() => setHovered(null)}
                    leftSection={
                      <span
                        aria-hidden="true"
                        style={{ width: 10, height: 10, background: category.color, display: "inline-block" }}
                      />
                    }
                  >
                    {category.label} · {money(category.total, allowance.currency)}
                  </Button>
                ))}
              </Group>
              <Text size="xs" c="dimmed" mt="xs">
                Select categories to highlight them; select again to clear each one. Leash level:{" "}
                {money(history.daily_reference_minor_units, allowance.currency)}/day.
              </Text>
            </details>
            <details>
              <summary>How this chart works</summary>
              <Text size="xs" c="dimmed">
                Bars include recorded positive flexible purchases, including pending and unclassified purchases. The
                moving averages use the same accounting and account-coverage checks as the desktop spending signal.
                Earlier points are reconstructed from current classifications and can change; provider data can lag. The
                dashed line is the monthly allowance expressed as a daily reference.
              </Text>
            </details>
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
