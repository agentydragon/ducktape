import { Badge, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import Decimal from "decimal.js";

import type { AllowancePeriodId } from "./types";

export const periodLabels = {
  credit_cycle: "Credit cycle",
  calendar_month: "Calendar month",
  year_to_date: "Year to date",
  rolling_7d: "Last 7 days",
  rolling_30d: "Last 30 days",
} satisfies Record<AllowancePeriodId, string>;

export function money(value: number | Decimal | null | undefined, currency: string | null, exact = false): string {
  if (value == null) return "Unavailable";
  const minorUnits = Decimal.isDecimal(value) ? value : new Decimal(value);
  if (!minorUnits.isFinite()) return "Unavailable";
  const code = currency?.length === 3 ? currency.toUpperCase() : "USD";
  try {
    const formatter = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: code,
      maximumFractionDigits: exact ? undefined : 0,
      minimumFractionDigits: exact ? undefined : 0,
    });
    const precision = new Intl.NumberFormat(undefined, { style: "currency", currency: code }).resolvedOptions()
      .maximumFractionDigits;
    const exactAmount = minorUnits.div(new Decimal(10).pow(precision ?? 2));
    if (!exact && !exactAmount.isZero() && exactAmount.abs().lessThan("0.5")) {
      return `${exactAmount.isNegative() ? "-" : ""}<${formatter.format(1)}`;
    }
    const displayAmount = exactAmount.toDecimalPlaces(exact ? (precision ?? 2) : 0, Decimal.ROUND_HALF_UP);
    return formatter.format(displayAmount.toNumber());
  } catch {
    const amount = minorUnits.div(100).toNumber();
    if (!exact && amount !== 0 && Math.abs(amount) < 0.5) return `${amount < 0 ? "-" : ""}<${code} 1`;
    return `${code} ${amount.toFixed(exact ? 2 : 0)}`;
  }
}

export function Money({ value, currency }: { value: number | null | undefined; currency: string | null }): ReactNode {
  return <span title={money(value, currency, true)}>{money(value, currency)}</span>;
}

export function time(value: string | null | undefined): string {
  if (!value || Number.isNaN(new Date(value).getTime())) return "Unknown";
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export type Signal = "normal" | "warning" | "exceeded";

export function SignalLabel({ signal }: { signal: Signal }) {
  return (
    <Badge color={signal === "exceeded" ? "red" : signal === "warning" ? "yellow" : "teal"} variant="light">
      {signal === "exceeded" ? "Over allowance" : signal === "warning" ? "Pace warning" : "Below provisional leash"}
    </Badge>
  );
}

export function Metric({ label, value, detail }: { label: string; value: ReactNode; detail?: string }) {
  return (
    <Stack gap={2}>
      <Text size="xs" c="dimmed" fw={600}>
        {label}
      </Text>
      <Text size="xl" fw={700}>
        {value}
      </Text>
      {detail && (
        <Text size="xs" c="dimmed">
          {detail}
        </Text>
      )}
    </Stack>
  );
}
