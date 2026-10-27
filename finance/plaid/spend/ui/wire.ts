import type { z } from "zod";

import {
  zSpendConfigurationView,
  zSpendTransactionsView,
  zSpendView,
  zSustainabilityDashboard,
} from "./api/schema.zod";
import type { SpendConfiguration, SpendView, SustainabilityDashboard, TransactionsView } from "./types";

function decode<T>(schema: z.ZodType<T>, text: string, label: string): T {
  let value: unknown;
  try {
    value = JSON.parse(text) as unknown;
  } catch {
    throw new Error(`Invalid ${label}: malformed JSON`);
  }
  const parsed = schema.safeParse(value);
  if (parsed.success) return parsed.data;
  const issue = parsed.error.issues[0];
  const path = issue?.path.length ? ` at ${issue.path.map(String).join(".")}` : "";
  throw new Error(`Invalid ${label}${path}: ${issue?.message ?? "schema mismatch"}`);
}

export function decodeSpendView(text: string): SpendView {
  return decode(zSpendView, text, "Spend view");
}

export function decodeSpendConfiguration(text: string): SpendConfiguration {
  return decode(zSpendConfigurationView, text, "Spend configuration");
}

export function decodeSpendTransactions(text: string): TransactionsView {
  return decode(zSpendTransactionsView, text, "Spend transactions");
}

export function decodeSustainabilityDashboard(text: string): SustainabilityDashboard {
  return decode(zSustainabilityDashboard, text, "Sustainability dashboard");
}
