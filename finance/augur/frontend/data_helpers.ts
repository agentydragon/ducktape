import { rowsFrom } from "./lib/frame";
import {
  currencyQuantaAdd,
  currencyQuantaChartNumber,
  currencyQuantaCompare,
  currencyQuantaIsPositive,
  fmtQuanta,
  fmtNumber,
} from "./lib/format";
import { METRIC_OPTIONS, sleeveKey, type LifecycleInputEvent, type productInputDefaults } from "./input_helpers";
import type {
  fetchCalibrationRun,
  fetchProductPortfolio,
  fetchProductProjectionSummary,
  fetchProductRollout,
} from "./client";
import { ROLLOUT_EVENT_KIND_ORDER, type RolloutEventKind } from "./rollout_event_vocabulary.generated";

export { ROLLOUT_EVENT_KIND_ORDER };

type ProductProjectionSummary = Awaited<ReturnType<typeof fetchProductProjectionSummary>>;
type ProductPortfolio = Awaited<ReturnType<typeof fetchProductPortfolio>>;
type ProductRolloutResponse = Awaited<ReturnType<typeof fetchProductRollout>>;
type CalibrationRun = Awaited<ReturnType<typeof fetchCalibrationRun>>;
type ProductMetricFan = ProductProjectionSummary["metric_fan"];
type ProductTerminalDistribution = ProductProjectionSummary["terminal_distribution"];
type ProductRollout = ProductRolloutResponse["rollout"];
type RolloutEvent = ProductRollout["events"][number];
type CurrencyMetadata = { currencyCode?: string; currencyQuantum?: string };
type RolloutEventWithCurrency = RolloutEvent & { _currency: CurrencyMetadata };
type EventKind = RolloutEvent["kind"];
type EventOfKind<Kind extends EventKind> = Extract<RolloutEventWithCurrency, { kind: Kind }>;
type CalibrationMarkFan = NonNullable<CalibrationRun["mark_fans"]>[number];
type MetricOption = (typeof METRIC_OPTIONS)[number];
type ProductInput = ReturnType<typeof productInputDefaults>;
type RolloutSummary = Pick<ProductRollout, "failed" | "ending_metrics">;
type DueWithShortfallEvent = EventOfKind<
  | "monthly_expense"
  | "outside_rent"
  | "tax_payment"
  | "property_tax_payment"
  | "hoa_dues_payment"
  | "homeowners_insurance_payment"
  | "property_maintenance_payment"
>;
type EventFormatterMap = {
  [Kind in EventKind]: {
    label: (event: EventOfKind<Kind>) => string;
    detail: (event: EventOfKind<Kind>) => string;
  };
};

function currency(result: { currency_code: string; currency_quantum: string } | null | undefined): CurrencyMetadata {
  return { currencyCode: result?.currency_code, currencyQuantum: result?.currency_quantum };
}

function cu(value: unknown, currencyMeta: CurrencyMetadata | undefined) {
  return fmtQuanta(value, currencyMeta);
}

export const SELECTED_ROLLOUT_COLOR = "#0f766e";
export const FAILED_ROLLOUT_COLOR = "#ef4444";
// Presentation stays frontend policy, but its keys are exhaustively checked against the
// backend-generated discriminator vocabulary. The generated order drives legends and marker stacks.
type RolloutEventMetadata = { label: string; color: string; hidden?: boolean };
const ROLLOUT_EVENT_METADATA_BY_KIND: Record<RolloutEventKind, RolloutEventMetadata> = {
  property_purchase: { label: "Property purchase", color: "#1d4ed8" },
  closing_cost_payment: { label: "Closing cost", color: "#7e22ce" },
  set_primary_residence: { label: "Set primary home", color: "#2563eb" },
  set_rented_fraction: { label: "Set rented %", color: "#0ea5e9" },
  capital_improvement: { label: "Capital improvement", color: "#15803d" },
  property_sale: { label: "Property sale", color: "#be123c" },
  private_equity_event: { label: "PE event", color: "#9333ea" },
  private_equity_opportunity: { label: "PE opportunity", color: "#6d28d9" },
  holding_sale: { label: "Holding sale", color: "#0f766e" },
  tlh_financial_effect: { label: "TLH portfolio", color: "#047857" },
  tax_accrual: { label: "Tax accrual", color: "#b45309" },
  tax_payment: { label: "Tax payment", color: "#7c3aed" },
  property_tax_payment: { label: "Property tax", color: "#a16207", hidden: true },
  hoa_dues_payment: { label: "HOA dues", color: "#14b8a6" },
  homeowners_insurance_payment: { label: "Homeowners insurance", color: "#9333ea", hidden: true },
  property_maintenance_payment: { label: "Maintenance", color: "#d97706", hidden: true },
  mortgage_payment: { label: "Mortgage payment", color: "#0369a1" },
  monthly_expense: { label: "Monthly expense", color: "#64748b", hidden: true },
  outside_rent: { label: "Outside rent", color: "#0891b2", hidden: true },
  failure: { label: "Rollout failure", color: "#dc2626" },
};

const ROLLOUT_EVENT_METADATA = ROLLOUT_EVENT_KIND_ORDER.map((kind) => ({
  kind,
  ...ROLLOUT_EVENT_METADATA_BY_KIND[kind],
}));

export const ROLLOUT_EVENT_KIND_LABELS = Object.fromEntries(
  ROLLOUT_EVENT_METADATA.map(({ kind, label }) => [kind, label])
);
export const ROLLOUT_EVENT_COLORS = Object.fromEntries(ROLLOUT_EVENT_METADATA.map(({ kind, color }) => [kind, color]));
export const DEFAULT_HIDDEN_EVENT_KINDS = new Set(
  ROLLOUT_EVENT_METADATA.filter(({ hidden }) => hidden).map(({ kind }) => kind)
);

// Pixel pitch between vertical marker stacks (events stack upward above the rollout line).
export const EVENT_MARKER_STACK_PITCH_PX = 12;
export const EVENT_MARKER_STACK_BASE_OFFSET_PX = -10;

export const TABLE_NUMERIC_CELL = "px-3 py-2 text-right augur-tabular";
export const TABLE_NUMERIC_HEADER = "px-3 py-2 text-right font-semibold";
// "Selected rollout" callout cells / headers in the percentile table — teal accent.
export const SELECTED_COL_HEADER = "px-3 py-2 text-right font-semibold text-teal-700 dark:text-teal-300";
export const SELECTED_COL_CELL = "px-3 py-2 text-right font-semibold text-teal-700 augur-tabular dark:text-teal-300";

export function metricFanRows(result: ProductMetricFan | null | undefined) {
  if (!result?.monthly_metric_fan) return [];
  const byMonth = new Map<number, Map<number, number>>();
  const displayValuesByMonth = new Map<number, Map<number, string | number | boolean>>();
  for (const row of rowsFrom(result.monthly_metric_fan)) {
    if (row.value_quanta == null) continue;
    const monthIndex = Number(row.month_index);
    const percentile = Number(row.percentile);
    const rawValue = row.value_quanta;
    const metricValue = currencyQuantaChartNumber(rawValue, result.currency_quantum);
    if (!Number.isFinite(monthIndex) || !Number.isFinite(percentile) || !Number.isFinite(metricValue)) continue;
    let monthValues = byMonth.get(monthIndex);
    if (monthValues == null) {
      monthValues = new Map();
      byMonth.set(monthIndex, monthValues);
    }
    let monthDisplayValues = displayValuesByMonth.get(monthIndex);
    if (monthDisplayValues == null) {
      monthDisplayValues = new Map();
      displayValuesByMonth.set(monthIndex, monthDisplayValues);
    }
    monthValues.set(percentile, metricValue);
    monthDisplayValues.set(percentile, rawValue);
  }
  return [...byMonth.entries()]
    .sort(([left], [right]) => left - right)
    .map(([monthIndex, values]) => ({
      monthIndex,
      year: monthIndex / 12,
      values,
      displayValues: displayValuesByMonth.get(monthIndex),
      currency: currency(result),
    }));
}

// Adapt a calibration `MarkFan` (`months: [{ month_index, values: { "5.0": float, ... } }]`)
// to the row shape `MetricFanChart` consumes: `{ monthIndex, year, values: Map<pct, value> }`.
// The fan's `percentiles` are numbers (5, 25, ...) while each month's `values` is keyed by the
// stringified percentile ("5.0", ...), so we re-key the Map by `Number(...)` to line up with the
// `percentiles` prop the chart sorts over (`Number("5.0") === 5`).
export function markFanRows(markFan: CalibrationMarkFan | null | undefined) {
  if (!markFan?.months) return [];
  return markFan.months
    .map((month) => ({
      monthIndex: Number(month.month_index),
      year: Number(month.month_index) / 12,
      values: new Map(
        Object.entries(month.values ?? {})
          .map(([percentile, value]) => [Number(percentile), Number(value)] as [number, number])
          .filter(([percentile, value]) => Number.isFinite(percentile) && Number.isFinite(value))
      ),
    }))
    .filter((row) => Number.isFinite(row.monthIndex))
    .sort((left, right) => left.monthIndex - right.monthIndex);
}

export function terminalPercentileValue(
  result: Pick<ProductTerminalDistribution, "terminal_metric_percentiles"> | null | undefined,
  percentile: number
) {
  if (!result?.terminal_metric_percentiles) return null;
  for (const row of rowsFrom(result?.terminal_metric_percentiles)) {
    if (Number(row.percentile) === percentile) {
      return row.value_quanta;
    }
  }
  return null;
}

export function terminalMetricSamples(result: ProductTerminalDistribution | null | undefined, metric: MetricOption) {
  if (result?.metric !== metric.value || !result?.terminal_metric_samples) return [];
  return rowsFrom(result.terminal_metric_samples)
    .filter((row) => row.value_quanta != null)
    .map((row) => ({
      seed: Number(row.seed),
      value: currencyQuantaChartNumber(row.value_quanta, result.currency_quantum),
      currencyQuanta: row.value_quanta,
      currency: currency(result),
      failed: Boolean(row.failed),
    }))
    .filter((row) => Number.isInteger(row.seed) && Number.isFinite(row.value));
}

export function terminalSampleAtPercentile(
  result: ProductTerminalDistribution | null | undefined,
  metric: MetricOption,
  percentile: number
) {
  const samples = terminalMetricSamples(result, metric)
    .slice()
    .sort((left, right) => currencyQuantaCompare(left.currencyQuanta, right.currencyQuanta) || left.seed - right.seed);
  if (samples.length === 0) return null;
  if (samples.length === 1) return samples[0];
  const rank = Math.floor((Math.max(0, Math.min(100, Number(percentile))) / 100) * (samples.length - 1) + 0.5);
  return samples[Math.max(0, Math.min(samples.length - 1, rank))];
}

export function endingMetricValue(
  endingMetrics: ProductRollout["ending_metrics"] | null | undefined,
  metric: MetricOption
) {
  return endingMetrics?.[metric.chartValue] ?? null;
}

const PROPERTY_METRIC_VALUES = new Set(["property_value"]);
const MORTGAGE_METRIC_VALUES = new Set(["mortgage_balance", "home_equity"]);

export function visibleMetricOptions(input: ProductInput | null | undefined) {
  const hasProperty = input?.propertyId != null;
  const hasMortgage = hasProperty && input?.financingKind === "mortgage";
  return METRIC_OPTIONS.filter((metric) => {
    if (!hasProperty && PROPERTY_METRIC_VALUES.has(metric.value)) return false;
    if (!hasMortgage && MORTGAGE_METRIC_VALUES.has(metric.value)) return false;
    return true;
  });
}

export function rolloutStatusText(summary: RolloutSummary | null | undefined) {
  if (!summary) return "No rollout selected";
  const failedMonth = summary.ending_metrics?.failed_month_index;
  if (summary.failed) return Number.isFinite(failedMonth) ? `failed m${failedMonth}` : "failed";
  return "completed";
}

export function selectedRolloutMetricRows(detail: ProductRolloutResponse | null | undefined, metric: MetricOption) {
  if (detail == null) return [];
  const rollout = detail.rollout;
  if (rollout == null || !rollout.monthly_metrics) return [];
  const failed = rollout.failed;
  const endingSnapshotIndex = rollout.ending_metrics?.snapshot_index;
  const endingFailedMonthIndex = rollout.ending_metrics?.failed_month_index;
  return rowsFrom(rollout.monthly_metrics)
    .map((row) => ({
      monthIndex: Number(row.month_index),
      // The stopped post-event book has not observed the next month's market mark.
      year:
        (failed && Number(row.month_index) === endingSnapshotIndex
          ? Number(endingFailedMonthIndex)
          : Number(row.month_index)) / 12,
      value: currencyQuantaChartNumber(row[metric.chartValue], detail.currency_quantum),
      currencyQuanta: row[metric.chartValue],
      currency: currency(detail),
    }))
    .filter((row) => Number.isFinite(row.monthIndex) && Number.isFinite(row.value));
}

export function selectedRolloutEvents(detail: ProductRolloutResponse | null | undefined): RolloutEventWithCurrency[] {
  return Array.isArray(detail?.rollout?.events)
    ? detail.rollout.events.map((event) => ({ ...event, _currency: currency(detail) }))
    : [];
}

export function eventMonthIndex(event: RolloutEvent | null | undefined) {
  const monthIndex = Number(event?.month_index);
  return Number.isFinite(monthIndex) ? monthIndex : null;
}

export function eventStateMonthIndex(event: RolloutEvent | null | undefined) {
  const monthIndex = eventMonthIndex(event);
  return monthIndex == null ? null : monthIndex + 1;
}

export function eventGroupsByMonth(events: readonly RolloutEventWithCurrency[]) {
  const groups = new Map<number, RolloutEventWithCurrency[]>();
  for (const event of events) {
    const monthIndex = eventMonthIndex(event);
    if (monthIndex == null) continue;
    let monthEvents = groups.get(monthIndex);
    if (monthEvents == null) {
      monthEvents = [];
      groups.set(monthIndex, monthEvents);
    }
    monthEvents.push(event);
  }
  return [...groups.entries()]
    .sort(([left], [right]) => left - right)
    .map(([monthIndex, monthEvents]) => ({ monthIndex, events: monthEvents }));
}

export function eventColor(event: RolloutEvent | null | undefined) {
  if (!event) return "#64748b";
  return ROLLOUT_EVENT_COLORS[event.kind] ?? "#64748b";
}

export function eventAmount(event: RolloutEvent | null | undefined) {
  return event?.amount_quanta ?? null;
}

export function jurisdictionLabel(jurisdictionId: EventOfKind<"tax_accrual">["jurisdiction_id"] | null | undefined) {
  if (jurisdictionId === "federal_us") return "federal";
  if (jurisdictionId === "california") return "California";
  return (jurisdictionId ?? "").replace(/_/g, " ");
}

export function formatDueWithShortfall(
  amountDueQuanta: unknown,
  shortfallQuanta: unknown,
  currencyMeta: CurrencyMetadata
) {
  const base = `due ${cu(amountDueQuanta, currencyMeta)}`;
  return currencyQuantaIsPositive(shortfallQuanta) ? `${base}; shortfall ${cu(shortfallQuanta, currencyMeta)}` : base;
}

export function shortfallLabel(event: DueWithShortfallEvent, { ok, shortfall }: { ok: string; shortfall: string }) {
  return currencyQuantaIsPositive(event.shortfall_quanta) ? shortfall : ok;
}

export function taxPaymentLabel(event: EventOfKind<"tax_payment">) {
  const isShortfall = currencyQuantaIsPositive(event.shortfall_quanta);
  if (event.obligation_type === "estimated_tax")
    return isShortfall ? "Estimated tax shortfall" : "Paid estimated taxes";
  if (event.obligation_type === "tax_true_up") return isShortfall ? "Tax true-up shortfall" : "Paid tax true-up";
  return isShortfall ? "Tax payment shortfall" : "Paid taxes";
}

export function taxAccrualDetail(event: EventOfKind<"tax_accrual">) {
  const gain = currencyQuantaAdd(event.ltcg_quanta, event.stcg_quanta);
  const parts = [
    `ordinary tax ${cu(event.ordinary_tax_quanta, event._currency)}`,
    `gain tax ${cu(event.capital_gain_tax_quanta, event._currency)}`,
    `gains ${cu(gain, event._currency)}`,
  ];
  if (currencyQuantaIsPositive(event.mortgage_interest_deduction_quanta)) {
    const usedItemized =
      currencyQuantaIsPositive(event.itemized_deduction_quanta) &&
      BigInt(event.itemized_deduction_quanta) > BigInt(event.standard_deduction_quanta);
    parts.push(`MID ${cu(event.mortgage_interest_deduction_quanta, event._currency)}`);
    parts.push(
      `deduction ${cu(usedItemized ? event.itemized_deduction_quanta : event.standard_deduction_quanta, event._currency)} (${usedItemized ? "itemized" : "standard"})`
    );
  }
  return parts.join("; ");
}

export function propertyPurchaseDetail(event: EventOfKind<"property_purchase">) {
  const parts = [`down ${cu(event.down_payment_quanta, event._currency)}`];
  if (currencyQuantaIsPositive(event.mortgage_principal_quanta)) {
    parts.push(`mortgage ${cu(event.mortgage_principal_quanta, event._currency)}`);
  }
  return parts.join("; ");
}

const dueWithShortfallDetail = (event: DueWithShortfallEvent) =>
  formatDueWithShortfall(event.amount_due_quanta, event.shortfall_quanta, event._currency);

// A human-friendly name for the typed `AssetKey` an event carries — the display fallback when
// no curated `assetLabel` is set. Derived from the kind's own identifying field (the security's
// symbol, the PE issuer), not the old `crypto:btc`-style wire string.
type EventAsset =
  | EventOfKind<"holding_sale">["asset"]
  | EventOfKind<"private_equity_event">["asset"]
  | EventOfKind<"private_equity_opportunity">["asset"];

function assetDisplayName(asset: EventAsset | null | undefined) {
  if (!asset) return undefined;
  switch (asset.kind) {
    case "security":
      return asset.symbol.toUpperCase();
    case "private_equity":
      return asset.issuer_id;
    default:
      return undefined;
  }
}

// The mapped type requires a formatter for every generated `RolloutEvent` discriminator.
const tlhOperationLabel: Record<EventOfKind<"tlh_financial_effect">["operation"], string> = {
  contribution: "contribution",
  redemption: "redemption",
  modeled_realization: "modeled realization",
  distribution: "distribution",
};

export const EVENT_FORMATTERS: EventFormatterMap = {
  holding_sale: {
    label: (event) => `Sold ${event.asset_label ?? assetDisplayName(event.asset) ?? "asset"}`,
    detail: (event) => `${fmtNumber(event.units)} units; basis ${cu(event.cost_basis_quanta, event._currency)}`,
  },
  tlh_financial_effect: {
    label: (event) => {
      const operation = tlhOperationLabel[event.operation];
      return `TLH ${operation}: ${event.portfolio_id}`;
    },
    detail: (event) =>
      `cash ${cu(event.amount_quanta, event._currency)}; ST gain ${cu(event.short_term_gain_quanta, event._currency)}; LT gain ${cu(event.long_term_gain_quanta, event._currency)}; basis change ${cu(event.basis_change_quanta, event._currency)}; income ${cu(event.income_quanta, event._currency)}`,
  },
  monthly_expense: {
    label: (event) => shortfallLabel(event, { ok: "Paid monthly expenses", shortfall: "Monthly expenses shortfall" }),
    detail: dueWithShortfallDetail,
  },
  outside_rent: {
    label: (event) => shortfallLabel(event, { ok: "Paid rent", shortfall: "Rent shortfall" }),
    detail: dueWithShortfallDetail,
  },
  tax_accrual: {
    label: (event) => `Accrued ${jurisdictionLabel(event.jurisdiction_id)} tax`,
    detail: taxAccrualDetail,
  },
  tax_payment: { label: taxPaymentLabel, detail: dueWithShortfallDetail },
  property_purchase: { label: () => "Bought property", detail: propertyPurchaseDetail },
  closing_cost_payment: { label: () => "Paid closing costs", detail: () => "" },
  mortgage_payment: {
    label: () => "Paid mortgage",
    detail: (event) =>
      `interest ${cu(event.interest_quanta, event._currency)}; principal ${cu(event.principal_quanta, event._currency)}`,
  },
  property_tax_payment: {
    label: (event) => shortfallLabel(event, { ok: "Paid property tax", shortfall: "Property tax shortfall" }),
    detail: dueWithShortfallDetail,
  },
  hoa_dues_payment: {
    label: (event) => shortfallLabel(event, { ok: "Paid HOA dues", shortfall: "HOA dues shortfall" }),
    detail: dueWithShortfallDetail,
  },
  homeowners_insurance_payment: {
    label: (event) =>
      shortfallLabel(event, { ok: "Paid homeowner's insurance", shortfall: "Homeowner's insurance shortfall" }),
    detail: dueWithShortfallDetail,
  },
  property_maintenance_payment: {
    label: (event) => shortfallLabel(event, { ok: "Paid maintenance", shortfall: "Maintenance shortfall" }),
    detail: dueWithShortfallDetail,
  },
  failure: {
    label: () => "Rollout failed",
    detail: (event) => `shortfall ${cu(event.shortfall_quanta, event._currency)}`,
  },
  set_rented_fraction: {
    label: (event) => {
      const fraction = Number(event.rented_fraction);
      if (fraction <= 0) return "Stopped renting";
      if (fraction >= 1) return "Started renting out fully";
      return `Set rented to ${(fraction * 100).toFixed(0)}%`;
    },
    detail: (event) => `${event.property_id}`,
  },
  set_primary_residence: {
    label: (event) => (event.is_primary_residence ? "Set primary home" : "Cleared primary home"),
    detail: (event) => event.property_id ?? event.agent_id ?? "",
  },
  capital_improvement: {
    label: () => "Capital improvement",
    detail: (event) => `${event.property_id}; basis bump ${cu(event.amount_quanta, event._currency)}`,
  },
  property_sale: {
    label: () => "Sold property",
    detail: (event) => {
      const parts = [
        `${event.property_id}`,
        `proceeds ${cu(event.gross_proceeds_quanta, event._currency)}`,
        `payoff ${cu(event.mortgage_payoff_quanta, event._currency)}`,
        `net cash ${cu(event.net_cash_to_owner_quanta, event._currency)}`,
      ];
      if (currencyQuantaIsPositive(event.depreciation_recapture_quanta)) {
        parts.push(`§1250 ${cu(event.depreciation_recapture_quanta, event._currency)}`);
      }
      if (currencyQuantaIsPositive(event.section_121_exclusion_quanta)) {
        parts.push(`§121 ${cu(event.section_121_exclusion_quanta, event._currency)}`);
      }
      if (currencyQuantaIsPositive(event.long_term_capital_gain_quanta)) {
        parts.push(`LTCG ${cu(event.long_term_capital_gain_quanta, event._currency)}`);
      }
      return parts.join("; ");
    },
  },
  private_equity_event: {
    label: (event) => {
      const label = event.asset_label ?? assetDisplayName(event.asset) ?? "Private equity";
      if (event.event_kind === "tender") return `Tender: ${label}`;
      if (event.event_kind === "public_market_open") return `Public market: ${label}`;
      if (event.event_kind === "acquisition_cashout") return `Acquisition: ${label}`;
      if (event.event_kind === "legal_impairment") return `Liquidity impaired: ${label}`;
      if (event.event_kind === "forced_recovery") return `Recovery cashout: ${label}`;
      if (event.event_kind === "collapse") return `Collapsed: ${label}`;
      return `PE event: ${label}`;
    },
    detail: (event) => {
      const parts = [`mark ${cu(event.mark_quanta, event._currency)}`, String(event.regime ?? "").replace(/_/g, " ")];
      const capacity = Number(event.sale_capacity_fraction);
      if (Number.isFinite(capacity) && capacity < 1) parts.push(`capacity ${(capacity * 100).toFixed(0)}%`);
      const eligible = Number(event.eligible_fraction);
      if (Number.isFinite(eligible) && eligible < 1) parts.push(`eligible ${(eligible * 100).toFixed(0)}%`);
      const forcedSale = Number(event.forced_sale_fraction);
      if (forcedSale > 0) parts.push(`forced sale ${(forcedSale * 100).toFixed(0)}%`);
      if (event.liquidity_blocked) parts.push("liquidity blocked");
      if (currencyQuantaIsPositive(event.forced_recovery_cashout_quanta)) {
        parts.push(`recovery ${cu(event.forced_recovery_cashout_quanta, event._currency)}`);
      }
      return parts.filter(Boolean).join("; ");
    },
  },
  private_equity_opportunity: {
    label: (event) => {
      const label = event.asset_label ?? assetDisplayName(event.asset) ?? "Private equity";
      const outcome = String(event.outcome ?? "").replace(/_/g, " ");
      return `PE opportunity: ${label}${outcome ? ` (${outcome})` : ""}`;
    },
    detail: (event) => {
      const parts = [
        `mark ${cu(event.mark_quanta, event._currency)}`,
        `shortfall ${cu(event.shortfall_quanta, event._currency)}`,
        `target ${fmtNumber(event.target_units)} units`,
      ];
      if (currencyQuantaIsPositive(event.proceeds_quanta)) {
        parts.push(`proceeds ${cu(event.proceeds_quanta, event._currency)}`);
      }
      const capacity = Number(event.sale_capacity_fraction);
      if (Number.isFinite(capacity) && capacity < 1) parts.push(`capacity ${(capacity * 100).toFixed(0)}%`);
      const eligible = Number(event.eligible_fraction);
      if (Number.isFinite(eligible) && eligible < 1) parts.push(`eligible ${(eligible * 100).toFixed(0)}%`);
      if (event.liquidity_blocked) parts.push("liquidity blocked");
      return parts.filter(Boolean).join("; ");
    },
  },
};

export function eventLabel(event: RolloutEventWithCurrency | null | undefined) {
  if (!event) return "Event";
  switch (event.kind) {
    case "holding_sale":
      return EVENT_FORMATTERS.holding_sale.label(event);
    case "tlh_financial_effect":
      return EVENT_FORMATTERS.tlh_financial_effect.label(event);
    case "monthly_expense":
      return EVENT_FORMATTERS.monthly_expense.label(event);
    case "outside_rent":
      return EVENT_FORMATTERS.outside_rent.label(event);
    case "tax_accrual":
      return EVENT_FORMATTERS.tax_accrual.label(event);
    case "tax_payment":
      return EVENT_FORMATTERS.tax_payment.label(event);
    case "property_purchase":
      return EVENT_FORMATTERS.property_purchase.label(event);
    case "closing_cost_payment":
      return EVENT_FORMATTERS.closing_cost_payment.label(event);
    case "mortgage_payment":
      return EVENT_FORMATTERS.mortgage_payment.label(event);
    case "property_tax_payment":
      return EVENT_FORMATTERS.property_tax_payment.label(event);
    case "hoa_dues_payment":
      return EVENT_FORMATTERS.hoa_dues_payment.label(event);
    case "homeowners_insurance_payment":
      return EVENT_FORMATTERS.homeowners_insurance_payment.label(event);
    case "property_maintenance_payment":
      return EVENT_FORMATTERS.property_maintenance_payment.label(event);
    case "failure":
      return EVENT_FORMATTERS.failure.label(event);
    case "set_rented_fraction":
      return EVENT_FORMATTERS.set_rented_fraction.label(event);
    case "set_primary_residence":
      return EVENT_FORMATTERS.set_primary_residence.label(event);
    case "capital_improvement":
      return EVENT_FORMATTERS.capital_improvement.label(event);
    case "property_sale":
      return EVENT_FORMATTERS.property_sale.label(event);
    case "private_equity_event":
      return EVENT_FORMATTERS.private_equity_event.label(event);
    case "private_equity_opportunity":
      return EVENT_FORMATTERS.private_equity_opportunity.label(event);
    default:
      return "Event";
  }
}

export function eventDetailText(event: RolloutEventWithCurrency | null | undefined) {
  if (!event) return "";
  switch (event.kind) {
    case "holding_sale":
      return EVENT_FORMATTERS.holding_sale.detail(event);
    case "tlh_financial_effect":
      return EVENT_FORMATTERS.tlh_financial_effect.detail(event);
    case "monthly_expense":
      return EVENT_FORMATTERS.monthly_expense.detail(event);
    case "outside_rent":
      return EVENT_FORMATTERS.outside_rent.detail(event);
    case "tax_accrual":
      return EVENT_FORMATTERS.tax_accrual.detail(event);
    case "tax_payment":
      return EVENT_FORMATTERS.tax_payment.detail(event);
    case "property_purchase":
      return EVENT_FORMATTERS.property_purchase.detail(event);
    case "closing_cost_payment":
      return EVENT_FORMATTERS.closing_cost_payment.detail(event);
    case "mortgage_payment":
      return EVENT_FORMATTERS.mortgage_payment.detail(event);
    case "property_tax_payment":
      return EVENT_FORMATTERS.property_tax_payment.detail(event);
    case "hoa_dues_payment":
      return EVENT_FORMATTERS.hoa_dues_payment.detail(event);
    case "homeowners_insurance_payment":
      return EVENT_FORMATTERS.homeowners_insurance_payment.detail(event);
    case "property_maintenance_payment":
      return EVENT_FORMATTERS.property_maintenance_payment.detail(event);
    case "failure":
      return EVENT_FORMATTERS.failure.detail(event);
    case "set_rented_fraction":
      return EVENT_FORMATTERS.set_rented_fraction.detail(event);
    case "set_primary_residence":
      return EVENT_FORMATTERS.set_primary_residence.detail(event);
    case "capital_improvement":
      return EVENT_FORMATTERS.capital_improvement.detail(event);
    case "property_sale":
      return EVENT_FORMATTERS.property_sale.detail(event);
    case "private_equity_event":
      return EVENT_FORMATTERS.private_equity_event.detail(event);
    case "private_equity_opportunity":
      return EVENT_FORMATTERS.private_equity_opportunity.detail(event);
    default:
      return "";
  }
}

export function eventTitle(event: RolloutEventWithCurrency) {
  return `Month ${eventStateMonthIndex(event) ?? "n/a"}: ${eventLabel(event)} ${cu(eventAmount(event), event?._currency)}`;
}

// Rows the target allocation may name, in portfolio order: held securities, then TLH portfolios.
// A security row is keyed by the SERIES symbol the sim acts on — not the holding's display
// ticker, which can differ (a VOO holding is priced by the SPY series) and which the backend
// would then fail to match, silently disabling auto-sale. Two holdings sharing one series
// collapse into a single row, because the sim cannot tell them apart either — so their values
// sum, which is what the weight seed has to divide by.
// Each TLH portfolio is a row of its own, named by its id and labelled with its own label: it is
// valued in money and drawn on as a managed portfolio, never merged with lots of its index.
// Private equity is absent because its key carries no symbol: it leaves only via a tender event.
export function sellableSleeves(portfolio: ProductPortfolio | null | undefined) {
  const bySymbol = new Map<string, { symbol: string; labels: string[]; valueQuanta: string | null }>();
  for (const position of portfolio?.holdings ?? []) {
    if (position.asset.kind === "private_equity") continue;
    const symbol = position.asset.symbol;
    if (!symbol) continue;
    const label = position.label || position.symbol || symbol;
    const valueQuanta = position.current_value_quanta ?? "0";
    const row = bySymbol.get(symbol);
    if (row) {
      row.labels.push(label);
      row.valueQuanta = currencyQuantaAdd(row.valueQuanta, valueQuanta);
    } else bySymbol.set(symbol, { symbol, labels: [label], valueQuanta });
  }
  const securities = [...bySymbol.values()].map(({ symbol, labels, valueQuanta }) =>
    sellableRow({ kind: "security", symbol }, labels.join(" + "), valueQuanta)
  );
  const managed = (portfolio?.tlh_portfolios ?? []).map((managedPortfolio) =>
    sellableRow(
      { kind: "managed_portfolio", portfolioId: managedPortfolio.portfolio_id },
      managedPortfolio.label,
      managedPortfolio.current_value_quanta ?? "0"
    )
  );
  return [...securities, ...managed];
}

function sellableRow(
  sleeve: { kind: "security"; symbol: string } | { kind: "managed_portfolio"; portfolioId: string },
  label: string,
  valueQuanta: string | null
) {
  return { key: sleeveKey(sleeve), sleeve, label, valueQuanta };
}

// Reads the typed asset key, not a display string: private equity is a different KIND of
// holding, and `securityKind` now describes only how to present a tradable security.
type PrivateEquityAsset = Extract<ProductPortfolio["holdings"][number]["asset"], { kind: "private_equity" }>;

export function isPrivateSecurityPosition(
  position: ProductPortfolio["holdings"][number]
): position is ProductPortfolio["holdings"][number] & { asset: PrivateEquityAsset } {
  return position?.asset?.kind === "private_equity";
}

export function firstSaleMonth(events: readonly LifecycleInputEvent[]): number | null {
  let earliest: number | null = null;
  for (const event of events) {
    if (event.kind === "property_sale" && (earliest == null || event.month < earliest)) {
      earliest = event.month;
    }
  }
  return earliest;
}

// True for any event the wire validator rejects as a post-sale residual: events strictly
// after `saleMonth`, plus same-month non-sale events (a SetRentedFraction in the same month
// as the sale is also illegal). `saleMonth == null` means no sale on the timeline → nothing
// is post-sale.
export function isEventPostSale(event: LifecycleInputEvent, saleMonth: number | null): boolean {
  if (saleMonth == null) return false;
  if (event.month > saleMonth) return true;
  return event.month === saleMonth && event.kind !== "property_sale";
}
