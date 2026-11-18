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
import { Legend } from "chart.js";

import { chartPalette, mountChart, seriesColor } from "./chart";
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
const RECENT_CHANGE_DAYS = 30;
const signedUsd = (minorUnits: number | null) => {
  return minorUnits != null && minorUnits > 0 ? `+${money(minorUnits, "USD")}` : money(minorUnits, "USD");
};

function NetWorthChart({ days }: NetWorthHistory) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scheme = useComputedColorScheme("light");

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const css = getComputedStyle(canvas);
    const color = (token: string) => css.getPropertyValue(`--mantine-color-${token}`).trim();
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
          x: { grid: { display: false }, ticks: { color: color("dimmed"), maxRotation: 0, maxTicksLimit: 8 } },
          y: {
            beginAtZero: false,
            grace: "5%",
            grid: { color: color("default-border") },
            ticks: { color: color("dimmed"), callback: (value) => usd.format(Number(value) / 100) },
          },
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

function AccountChangesChart({ days, accountLabels }: { days: NetWorthHistory["days"]; accountLabels: string[] }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scheme = useComputedColorScheme("light");
  const [range, setRange] = useState("recent");
  const firstVisibleIndex = range === "all" ? 0 : Math.max(0, days.length - RECENT_CHANGE_DAYS);
  const visibleDays = useMemo(() => days.slice(firstVisibleIndex), [days, firstVisibleIndex]);
  const hasChanges = visibleDays.some((day) => day.account_changes_minor_units != null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const css = getComputedStyle(canvas);
    const color = (token: string) => css.getPropertyValue(`--mantine-color-${token}`).trim();
    return mountChart(canvas, {
      type: "bar",
      plugins: [Legend],
      data: {
        labels: visibleDays.map((day) => dayLabel(day.date)),
        datasets: accountLabels.map((label, index) => ({
          label,
          backgroundColor: seriesColor(scheme, index),
          data: visibleDays.map((day) => day.account_changes_minor_units?.[index] ?? null),
        })),
      },
      options: {
        plugins: {
          legend: { position: "bottom", onClick: () => {}, labels: { color: color("text"), boxWidth: 12 } },
          tooltip: {
            filter: ({ parsed }) => parsed.y !== 0,
            callbacks: {
              label: ({ dataset, parsed }) => `${dataset.label} ${signedUsd(parsed.y)}`,
              footer: (items) => {
                const index = items[0]?.dataIndex;
                if (index == null) return "";
                const current = visibleDays[index].totals;
                const previous = days[firstVisibleIndex + index - 1]?.totals;
                return current && previous
                  ? `Net change ${signedUsd(current.net_minor_units - previous.net_minor_units)}`
                  : "";
              },
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: { color: color("dimmed"), maxRotation: 0, maxTicksLimit: 8 },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            grid: { color: color("default-border") },
            ticks: { color: color("dimmed"), callback: (value) => usd.format(Number(value) / 100) },
          },
        },
      },
    });
  }, [accountLabels, days, firstVisibleIndex, scheme, visibleDays]);

  return (
    <Stack component="section" aria-labelledby="account-changes-title" gap="xs">
      <Group justify="space-between">
        <Title id="account-changes-title" order={2} size="h3">
          Daily change by account
        </Title>
        {days.length > RECENT_CHANGE_DAYS && (
          <SegmentedControl
            aria-label="Account change history"
            size="xs"
            value={range}
            onChange={setRange}
            data={[
              { value: "recent", label: `${RECENT_CHANGE_DAYS} days` },
              { value: "all", label: "All history" },
            ]}
          />
        )}
      </Group>
      <Text size="sm" c="dimmed">
        Changes use consecutive days with complete balances. Transfers can show up on both sides of zero.
      </Text>
      {hasChanges ? (
        <Box h={{ base: 280, sm: 320 }}>
          <canvas ref={canvasRef} role="img" aria-label="Daily changes in net worth by account in US dollars" />
        </Box>
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
  const first = days?.[0];
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
        ) : history && first && latest?.totals ? (
          <>
            <div>
              <Text size="xl" fw={700}>
                {money(latest.totals.net_minor_units, "USD")}
              </Text>
              <Text size="sm" c="dimmed">
                {first === latest ? dayLabel(latest.date) : `${dayLabel(first.date)} – ${dayLabel(latest.date)}`}
              </Text>
            </div>
            <NetWorthChart days={days} />
            {days.some((day) => day.totals == null) && (
              <Text size="sm" c="dimmed">
                Gaps mark days with missing balances or observations more than an hour apart.
              </Text>
            )}
            {Boolean(history.account_labels?.length) && days.some((day) => day.account_changes_minor_units != null) && (
              <AccountChangesChart days={days} accountLabels={history.account_labels ?? []} />
            )}
          </>
        ) : (
          <Text c="dimmed">No complete balance history is available yet.</Text>
        )}
      </Stack>
    </Paper>
  );
}
