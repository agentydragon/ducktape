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

export const linkedSyncDescription =
  "The oldest successful sync of a Plaid Item (bank connection) used here. It may update transactions only; this tracks our local mirror, not when the bank last refreshed.";

export function money(value: number | Decimal | null | undefined, currency: string | null, exact = false): string {
  if (value == null) return "Unavailable";
  const minorUnits = Decimal.isDecimal(value) ? value : new Decimal(value);
  if (!minorUnits.isFinite()) return "Unavailable";
  const code = currency?.length === 3 ? currency.toUpperCase() : "USD";
  try {
    const precision = new Intl.NumberFormat("en-US", { style: "currency", currency: code }).resolvedOptions()
      .maximumFractionDigits;
    const exactAmount = minorUnits.div(new Decimal(10).pow(precision ?? 2));
    const digits =
      exact || (!exactAmount.isZero() && exactAmount.abs().lessThan(10) && !exactAmount.isInteger())
        ? (precision ?? 2)
        : 0;
    const formatter = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: code,
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
    const unit = new Decimal(10).pow(-(precision ?? 2));
    if (!exactAmount.isZero() && exactAmount.abs().lessThan(unit.div(2))) {
      return `${exactAmount.isNegative() ? "-" : ""}<${formatter.format(unit.toNumber())}`;
    }
    const displayAmount = exactAmount.toDecimalPlaces(digits, Decimal.ROUND_HALF_UP);
    return formatter.format(displayAmount.toNumber());
  } catch {
    const amount = minorUnits.div(100).toNumber();
    if (!exact && amount !== 0 && Math.abs(amount) < 0.005) return `${amount < 0 ? "-" : ""}<${code} 0.01`;
    return `${code} ${amount.toFixed(exact || (amount !== 0 && Math.abs(amount) < 10 && !Number.isInteger(amount)) ? 2 : 0)}`;
  }
}

export function Money({ value, currency }: { value: number | null | undefined; currency: string | null }): ReactNode {
  return <span title={money(value, currency, true)}>{money(value, currency)}</span>;
}

export function time(value: string | null | undefined, now = new Date()): string {
  if (!value || Number.isNaN(new Date(value).getTime())) return "Unknown";
  const date = new Date(value);
  const label = date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
  const clock = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  return Math.abs(date.getTime() - now.getTime()) <= 14 * 86400_000 ? `${label}, ${clock}` : label;
}

export type Signal = "normal" | "warning" | "exceeded";

export function signalText(signal: Signal, remainingMinorUnits: number | null): string {
  if (signal === "exceeded")
    return remainingMinorUnits !== null && remainingMinorUnits < 0 ? "Over allowance" : "Allowance exhausted";
  if (signal === "warning") return "Forecast or pace warning";
  return "Pace ≤ provisional leash";
}

export function SignalLabel({ signal, remainingMinorUnits }: { signal: Signal; remainingMinorUnits: number | null }) {
  return (
    <Badge color={signal === "exceeded" ? "red" : signal === "warning" ? "yellow" : "teal"} variant="light">
      {signalText(signal, remainingMinorUnits)}
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
