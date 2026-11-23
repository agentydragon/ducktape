import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Group,
  Paper,
  SegmentedControl,
  Skeleton,
  Stack,
  Text,
  Title,
  useComputedColorScheme,
} from "@mantine/core";
import { type Scale } from "chart.js";

import { chartPalette, mountChart, seriesColor } from "./chart";
import { ChartLegend, toggleLegendItem } from "./chart_legend";
import type { NetWorthHistory } from "./schema";
import { money } from "./spend_display";

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact" });
const shortDay = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric" });
const datedDay = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric", year: "numeric" });
const dayLabel = (day: string) => {
  const date = new Date(`${day}T00:00:00Z`);
  return (date.getUTCFullYear() === new Date().getFullYear() ? shortDay : datedDay).format(date);
};
const observedTime = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZoneName: "short" });
const RECENT_DAYS = 30;
const DOLLAR_AXIS_WIDTH = 64;
type NetWorthDay = NetWorthHistory["days"][number];

function dateChartScales(color: (token: string) => string) {
  return {
    x: {
      // Center the line points in the same date slots as the bars.
      offset: true,
      grid: { display: false },
      ticks: { color: color("dimmed"), maxRotation: 0, maxTicksLimit: 8 },
    },
    y: {
      // The two dollar axes have different magnitudes but share the same gutter.
      afterFit: (scale: Scale) => {
        scale.width = DOLLAR_AXIS_WIDTH;
      },
      grid: { color: color("default-border") },
      ticks: { color: color("dimmed"), callback: (value: string | number) => usd.format(Number(value) / 100) },
    },
  };
}

const signedUsd = (minorUnits: number | null) => {
  return minorUnits != null && minorUnits > 0 ? `+${money(minorUnits, "USD")}` : money(minorUnits, "USD");
};

function NetWorthChart({ days }: { days: NetWorthDay[] }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scheme = useComputedColorScheme("light");

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const css = getComputedStyle(canvas);
    const color = (token: string) => css.getPropertyValue(`--mantine-color-${token}`).trim();
    const scales = dateChartScales(color);
    return mountChart(canvas, {
      type: "line",
      data: {
        labels: days.map((day) => dayLabel(day.date)),
        datasets: [
          {
            label: "Net worth",
            data: days.map((day) => day.totals?.net_minor_units ?? null),
            borderColor: chartPalette[scheme].netWorth,
            backgroundColor: chartPalette[scheme].netWorth,
            borderWidth: 3,
          },
        ],
      },
      options: {
        layout: { autoPadding: false },
        elements: { point: { radius: days.length < 3 ? 3 : 1, hoverRadius: 4 } },
        plugins: {
          tooltip: {
            callbacks: {
              title: (items) => (items[0] ? dayLabel(days[items[0].dataIndex].date) : ""),
              label: ({ parsed }) => money(parsed.y, "USD"),
              footer: (items) => {
                const totals = items[0] ? days[items[0].dataIndex].totals : null;
                if (!totals) return "";
                const from = new Date(totals.observed_from);
                const through = new Date(totals.observed_through);
                const lines = [`Observed ${observedTime.formatRange(from, through)}`];
                const inferred = totals.inferred_zero_account_count ?? 0;
                if (inferred > 0) {
                  lines.push(
                    `Account history establishes zero for ${inferred} ${inferred === 1 ? "account" : "accounts"}.`
                  );
                }
                return lines;
              },
            },
          },
        },
        scales: {
          x: scales.x,
          y: { ...scales.y, beginAtZero: false, grace: "5%" },
        },
      },
    });
  }, [days, scheme]);

  return (
    <Box h={{ base: 280, sm: 360 }}>
      <canvas ref={canvasRef} role="img" aria-label="Net worth over time in US dollars" />
    </Box>
  );
}

function AccountChangesChart({
  days,
  accountLabels,
  previousDay,
}: {
  days: NetWorthDay[];
  accountLabels: string[];
  previousDay: NetWorthDay | undefined;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scheme = useComputedColorScheme("light");
  const hasChanges = days.some((day) => day.account_changes_minor_units != null);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [hovered, setHovered] = useState<string | null>(null);
  const highlighted = useMemo(() => new Set([...selected, ...(hovered ? [hovered] : [])]), [selected, hovered]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const css = getComputedStyle(canvas);
    const color = (token: string) => css.getPropertyValue(`--mantine-color-${token}`).trim();
    const scales = dateChartScales(color);
    return mountChart(canvas, {
      type: "bar",
      data: {
        labels: days.map((day) => dayLabel(day.date)),
        datasets: accountLabels.map((label, index) => {
          const base = seriesColor(scheme, index);
          const muted = base.startsWith("#") ? `${base}55` : base.replace(")", " / 0.33)");
          return {
            label,
            backgroundColor: highlighted.size > 0 && !highlighted.has(String(index)) ? muted : base,
            borderColor: highlighted.has(String(index)) ? chartPalette[scheme].emphasis : base,
            borderWidth: highlighted.has(String(index)) ? 2 : 0,
            borderSkipped: false as const,
            data: days.map((day) => day.account_changes_minor_units?.[index] ?? null),
          };
        }),
      },
      options: {
        layout: { autoPadding: false },
        plugins: {
          tooltip: {
            filter: ({ parsed }) => parsed.y !== 0,
            callbacks: {
              label: ({ dataset, parsed }) => `${dataset.label} ${signedUsd(parsed.y)}`,
              footer: (items) => {
                const index = items[0]?.dataIndex;
                if (index == null) return "";
                const current = days[index].totals;
                const previous = (index === 0 ? previousDay : days[index - 1])?.totals;
                return current && previous
                  ? `Net change ${signedUsd(current.net_minor_units - previous.net_minor_units)}`
                  : "";
              },
            },
          },
        },
        scales: {
          x: { ...scales.x, stacked: true },
          y: { ...scales.y, stacked: true, beginAtZero: true },
        },
      },
    });
  }, [accountLabels, days, highlighted, previousDay, scheme]);

  return (
    <Stack component="section" aria-labelledby="account-changes-title" gap="xs">
      <Title id="account-changes-title" order={2} size="h3">
        Daily change by account
      </Title>
      <Text size="sm" c="dimmed">
        Changes use consecutive days with complete balances. Transfers can show up on both sides of zero.
      </Text>
      {hasChanges ? (
        <>
          <Box h={{ base: 280, sm: 320 }}>
            <canvas
              ref={canvasRef}
              role="img"
              aria-label="Daily changes in net worth by account in US dollars"
              data-highlighted-accounts={[...highlighted].sort().join(",")}
            />
          </Box>
          <details open>
            <summary>Accounts</summary>
            <ChartLegend
              items={accountLabels.map((label, index) => ({
                id: String(index),
                label,
                color: seriesColor(scheme, index),
              }))}
              selected={selected}
              onToggle={(id) => setSelected((current) => toggleLegendItem(current, id))}
              onHover={setHovered}
            />
            <Text size="xs" c="dimmed" mt="xs">
              Select accounts to highlight them; select again to clear each one.
            </Text>
          </details>
        </>
      ) : (
        <Text size="sm" c="dimmed">
          No consecutive days have complete balances in this range.
        </Text>
      )}
    </Stack>
  );
}

export function NetWorthPage() {
  const [history, setHistory] = useState<NetWorthHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState("recent");

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/v1/net-worth", { signal: controller.signal });
        if (!response.ok) {
          throw new Error(
            response.status === 401 ? "Sign in again to see net worth." : "Net worth could not be loaded."
          );
        }
        const result = (await response.json()) as NetWorthHistory;
        if (!controller.signal.aborted) setHistory(result);
      } catch (reason) {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : "Net worth could not be loaded.");
      }
    }
    void load();
    return () => controller.abort();
  }, []);

  const days = history?.days;
  const firstVisibleIndex = range === "all" ? 0 : Math.max(0, (days?.length ?? 0) - RECENT_DAYS);
  const visibleDays = useMemo(() => days?.slice(firstVisibleIndex) ?? [], [days, firstVisibleIndex]);
  const first = visibleDays[0];
  const last = visibleDays.at(-1);
  const latest = days?.filter((day) => day.totals != null).at(-1);
  return (
    <Paper
      component="section"
      aria-label="Linked net worth history"
      withBorder
      radius="lg"
      p={{ base: "md", sm: "lg" }}
    >
      <Stack gap="md">
        <Text size="sm" c="dimmed">
          Plaid-linked USD accounts only, as far back as their balances can be combined.
        </Text>
        {error ? (
          <Alert color="red" role="alert">
            {error}
          </Alert>
        ) : days == null ? (
          <Skeleton h={280} role="status" aria-label="Loading net worth" />
        ) : history && first && last && latest?.totals ? (
          <>
            <Group justify="space-between">
              <div>
                <Text size="xl" fw={700}>
                  {money(latest.totals.net_minor_units, "USD")}
                </Text>
                <Text size="sm" c="dimmed">
                  {latest !== last && `Latest balance ${dayLabel(latest.date)} · `}
                  {first === last ? dayLabel(last.date) : `${dayLabel(first.date)} – ${dayLabel(last.date)}`}
                </Text>
              </div>
              {days.length > RECENT_DAYS && (
                <SegmentedControl
                  aria-label="Net worth date range"
                  size="xs"
                  value={range}
                  onChange={setRange}
                  data={[
                    { value: "recent", label: `${RECENT_DAYS} days` },
                    { value: "all", label: "All history" },
                  ]}
                />
              )}
            </Group>
            <NetWorthChart days={visibleDays} />
            {visibleDays.some((day) => day.totals == null) && (
              <Text size="sm" c="dimmed">
                Gaps mark days with missing balances or observations more than an hour apart.
              </Text>
            )}
            {Boolean(history.account_labels?.length) && days.some((day) => day.account_changes_minor_units != null) && (
              <AccountChangesChart
                days={visibleDays}
                accountLabels={history.account_labels ?? []}
                previousDay={days[firstVisibleIndex - 1]}
              />
            )}
          </>
        ) : (
          <Text c="dimmed">No complete balance history is available yet.</Text>
        )}
      </Stack>
    </Paper>
  );
}
