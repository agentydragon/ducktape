import { useEffect, useRef, useState } from "react";
import { Alert, Box, Paper, Skeleton, Stack, Text, Title, useComputedColorScheme } from "@mantine/core";

import { mountChart } from "./chart";
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
            borderColor: color("teal-filled"),
            backgroundColor: color("teal-filled"),
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
    <Paper component="section" aria-labelledby="net-worth-title" withBorder radius="lg" p={{ base: "md", sm: "lg" }}>
      <Stack gap="md">
        <Stack gap={4}>
          <Title id="net-worth-title" order={1} size="h2">
            Net worth
          </Title>
          <Text size="sm" c="dimmed">
            Plaid-linked USD accounts only, as far back as their balances can be combined.
          </Text>
        </Stack>
        {error ? (
          <Alert color="red" role="alert">
            {error}
          </Alert>
        ) : days == null ? (
          <Skeleton h={280} role="status" aria-label="Loading net worth" />
        ) : first && latest?.totals ? (
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
          </>
        ) : (
          <Text c="dimmed">No complete balance history is available yet.</Text>
        )}
      </Stack>
    </Paper>
  );
}
