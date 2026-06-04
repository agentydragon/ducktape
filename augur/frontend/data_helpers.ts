import { rowsFrom } from "./lib/frame.ts";
import { fmtNumber, fmtUsd, fmtUsdCompact } from "./lib/format.ts";
import { METRIC_OPTIONS, FAN_PERCENTILES } from "./input_helpers.ts";

function cu(value, currencyDisplay) {
  return currencyDisplay === "compact" ? fmtUsdCompact(value) : fmtUsd(value);
}

export const SELECTED_ROLLOUT_COLOR = "#0f766e";
export const FAILED_ROLLOUT_COLOR = "#ef4444";
// Canonical order of event kinds — drives both the legend's chip order and the in-month
// vertical stacking order on the chart. Mirrors `priority` in `augur/product/decode.py`
// so the wire-emit order and the visual order agree (the decoder already sorts events by
// (month_index, priority[kind]) before sending).
export const ROLLOUT_EVENT_KIND_ORDER = [
  "property_purchase",
  "closing_cost_payment",
  "set_primary_residence",
  "set_rented_fraction",
  "capital_improvement",
  "property_sale",
  "private_equity_event",
  "private_equity_opportunity",
  "holding_sale",
  "tax_accrual",
  "tax_payment",
  "property_tax_payment",
  "hoa_dues_payment",
  "homeowners_insurance_payment",
  "property_maintenance_payment",
  "mortgage_payment",
  "monthly_expense",
  "outside_rent",
  "failure",
];

export const ROLLOUT_EVENT_KIND_LABELS = {
  property_purchase: "Property purchase",
  closing_cost_payment: "Closing cost",
  set_primary_residence: "Set primary home",
  set_rented_fraction: "Set rented %",
  capital_improvement: "Capital improvement",
  property_sale: "Property sale",
  private_equity_event: "PE event",
  private_equity_opportunity: "PE opportunity",
  holding_sale: "Holding sale",
  tax_accrual: "Tax accrual",
  tax_payment: "Tax payment",
  property_tax_payment: "Property tax",
  hoa_dues_payment: "HOA dues",
  homeowners_insurance_payment: "Homeowners insurance",
  property_maintenance_payment: "Maintenance",
  mortgage_payment: "Mortgage payment",
  monthly_expense: "Monthly expense",
  outside_rent: "Outside rent",
  failure: "Rollout failure",
};

// Kinds that fire every month produce one marker per row at the same x position — visual
// clutter rather than signal. They start hidden in the legend; users can toggle them back on
// if they want to confirm the per-month accrual is firing.
export const DEFAULT_HIDDEN_EVENT_KINDS = new Set([
  "monthly_expense",
  "outside_rent",
  "property_tax_payment",
  "homeowners_insurance_payment",
  "property_maintenance_payment",
]);

export const ROLLOUT_EVENT_COLORS = {
  holding_sale: "#0f766e",
  monthly_expense: "#64748b",
  outside_rent: "#0891b2",
  property_purchase: "#1d4ed8",
  closing_cost_payment: "#7e22ce",
  mortgage_payment: "#0369a1",
  property_tax_payment: "#a16207",
  hoa_dues_payment: "#14b8a6",
  homeowners_insurance_payment: "#9333ea",
  property_maintenance_payment: "#d97706",
  tax_accrual: "#b45309",
  tax_payment: "#7c3aed",
  failure: "#dc2626",
  set_primary_residence: "#2563eb",
  set_rented_fraction: "#0ea5e9",
  capital_improvement: "#15803d",
  property_sale: "#be123c",
  private_equity_event: "#9333ea",
  private_equity_opportunity: "#6d28d9",
};

// Pixel pitch between vertical marker stacks (events stack upward above the rollout line).
export const EVENT_MARKER_STACK_PITCH_PX = 12;
export const EVENT_MARKER_STACK_BASE_OFFSET_PX = -10;

export const TABLE_NUMERIC_CELL = "px-3 py-2 text-right augur-tabular";
export const TABLE_NUMERIC_HEADER = "px-3 py-2 text-right font-semibold";
// "Selected rollout" callout cells / headers in the percentile table — teal accent.
export const SELECTED_COL_HEADER = "px-3 py-2 text-right font-semibold text-teal-700 dark:text-teal-300";
export const SELECTED_COL_CELL = "px-3 py-2 text-right font-semibold text-teal-700 augur-tabular dark:text-teal-300";

export function metricFanRows(result) {
  if (!result?.monthlyMetricFan) return [];
  const byMonth = new Map();
  for (const row of rowsFrom(result?.monthlyMetricFan)) {
    const monthIndex = Number(row.monthIndex);
    const percentile = Number(row.percentile);
    const metricValue = Number(row.value);
    if (!Number.isFinite(monthIndex) || !Number.isFinite(percentile) || !Number.isFinite(metricValue)) continue;
    if (!byMonth.has(monthIndex)) byMonth.set(monthIndex, new Map());
    byMonth.get(monthIndex).set(percentile, metricValue);
  }
  return [...byMonth.entries()]
    .sort(([left], [right]) => left - right)
    .map(([monthIndex, values]) => ({
      monthIndex,
      year: monthIndex / 12,
      values,
    }));
}

// Adapt a calibration `MarkFan` (`months: [{ monthIndex, values: { "5.0": float, ... } }]`)
// to the row shape `MetricFanChart` consumes: `{ monthIndex, year, values: Map<pct, value> }`.
// The fan's `percentiles` are numbers (5, 25, ...) while each month's `values` is keyed by the
// stringified percentile ("5.0", ...), so we re-key the Map by `Number(...)` to line up with the
// `percentiles` prop the chart sorts over (`Number("5.0") === 5`).
export function markFanRows(markFan) {
  if (!markFan?.months) return [];
  return markFan.months
    .map((month) => ({
      monthIndex: Number(month.monthIndex),
      year: Number(month.monthIndex) / 12,
      values: new Map(
        Object.entries(month.values ?? {})
          .map(([percentile, value]) => [Number(percentile), Number(value)] as [number, number])
          .filter(([percentile, value]) => Number.isFinite(percentile) && Number.isFinite(value))
      ),
    }))
    .filter((row) => Number.isFinite(row.monthIndex))
    .sort((left, right) => left.monthIndex - right.monthIndex);
}

export function terminalPercentileValue(result, percentile) {
  if (!result?.terminalMetricPercentiles) return null;
  for (const row of rowsFrom(result?.terminalMetricPercentiles)) {
    if (Number(row.percentile) === percentile) {
      return Number(row.value);
    }
  }
  return null;
}

export function terminalMetricValue(terminalMetrics, metric) {
  return Number(terminalMetrics?.[metric.chartValue]);
}

export function quantile(values, percentile) {
  const sorted = values
    .filter(Number.isFinite)
    .slice()
    .sort((left, right) => left - right);
  if (sorted.length === 0) return null;
  if (sorted.length === 1) return sorted[0];
  const position = (percentile / 100) * (sorted.length - 1);
  const lowerIndex = Math.floor(position);
  const upperIndex = Math.ceil(position);
  if (lowerIndex === upperIndex) return sorted[lowerIndex];
  const weight = position - lowerIndex;
  return sorted[lowerIndex] * (1 - weight) + sorted[upperIndex] * weight;
}

const PROPERTY_METRIC_VALUES = new Set(["property_value_usd"]);
const MORTGAGE_METRIC_VALUES = new Set(["mortgage_balance_usd", "home_equity_usd"]);

export function visibleMetricOptions(input) {
  const hasProperty = input?.propertyId != null;
  const hasMortgage = hasProperty && input?.financingKind === "mortgage";
  return METRIC_OPTIONS.filter((metric) => {
    if (!hasProperty && PROPERTY_METRIC_VALUES.has(metric.value)) return false;
    if (!hasMortgage && MORTGAGE_METRIC_VALUES.has(metric.value)) return false;
    return true;
  });
}

export function terminalMetricTableRows(summaries, selectedSummary, metrics) {
  return metrics.map((metric) => ({
    metric,
    percentiles: FAN_PERCENTILES.map((percentile) => ({
      percentile,
      value: quantile(
        summaries.map((summary) => terminalMetricValue(summary.terminalMetrics, metric)),
        percentile
      ),
    })),
    selectedValue: selectedSummary ? terminalMetricValue(selectedSummary.terminalMetrics, metric) : null,
  }));
}

export function rolloutStatusText(summary) {
  if (!summary) return "No rollout selected";
  const failedMonth = summary.terminalMetrics?.failedMonthIndex;
  if (summary.failed) return Number.isFinite(failedMonth) ? `failed m${failedMonth}` : "failed";
  return "completed";
}

export function selectedRolloutMetricRows(detail, metric) {
  if (!detail?.rollout?.monthlyMetrics) return [];
  return rowsFrom(detail.rollout.monthlyMetrics)
    .map((row) => ({
      monthIndex: Number(row.monthIndex),
      year: Number(row.monthIndex) / 12,
      value: Number(row[metric.chartValue]),
    }))
    .filter((row) => Number.isFinite(row.monthIndex) && Number.isFinite(row.value));
}

export function selectedRolloutEvents(detail) {
  return Array.isArray(detail?.rollout?.events) ? detail.rollout.events : [];
}

export function eventMonthIndex(event) {
  const monthIndex = Number(event?.monthIndex);
  return Number.isFinite(monthIndex) ? monthIndex : null;
}

export function eventStateMonthIndex(event) {
  const monthIndex = eventMonthIndex(event);
  return monthIndex == null ? null : monthIndex + 1;
}

export function eventGroupsByMonth(events) {
  const groups = new Map();
  for (const event of events) {
    const monthIndex = eventMonthIndex(event);
    if (monthIndex == null) continue;
    if (!groups.has(monthIndex)) groups.set(monthIndex, []);
    groups.get(monthIndex).push(event);
  }
  return [...groups.entries()]
    .sort(([left], [right]) => left - right)
    .map(([monthIndex, monthEvents]) => ({ monthIndex, events: monthEvents }));
}

export function eventColor(event) {
  return ROLLOUT_EVENT_COLORS[event?.kind] ?? "#64748b";
}

export function eventAmount(event) {
  return Number(event?.amountUsd);
}

export function jurisdictionLabel(jurisdictionId) {
  if (jurisdictionId === "federal_us") return "federal";
  if (jurisdictionId === "california") return "California";
  return (jurisdictionId ?? "").replace(/_/g, " ");
}

export function formatDueWithShortfall(amountDueUsd, shortfallUsd, currencyDisplay) {
  const shortfall = Number(shortfallUsd);
  const base = `due ${cu(amountDueUsd, currencyDisplay)}`;
  return shortfall > 0 ? `${base}; shortfall ${cu(shortfall, currencyDisplay)}` : base;
}

export function shortfallLabel(event, { ok, shortfall }) {
  return Number(event.shortfallUsd) > 0 ? shortfall : ok;
}

export function taxPaymentLabel(event) {
  const isShortfall = Number(event.shortfallUsd) > 0;
  if (event.obligationType === "estimated_tax") return isShortfall ? "Estimated tax shortfall" : "Paid estimated taxes";
  if (event.obligationType === "tax_true_up") return isShortfall ? "Tax true-up shortfall" : "Paid tax true-up";
  return isShortfall ? "Tax payment shortfall" : "Paid taxes";
}

export function taxAccrualDetail(event, currencyDisplay) {
  const capitalGainTax = Number(event.capitalGainTaxUsd);
  const gain = Number(event.ltcgUsd) + Number(event.stcgUsd);
  const itemized = Number(event.itemizedDeductionUsd);
  const standard = Number(event.standardDeductionUsd);
  const mid = Number(event.mortgageInterestDeductionUsd);
  const parts = [
    `ordinary tax ${cu(event.ordinaryTaxUsd, currencyDisplay)}`,
    `gain tax ${cu(capitalGainTax, currencyDisplay)}`,
    `gains ${cu(gain, currencyDisplay)}`,
  ];
  if (mid > 0) {
    const usedItemized = itemized > standard;
    parts.push(`MID ${cu(mid, currencyDisplay)}`);
    parts.push(
      `deduction ${cu(usedItemized ? itemized : standard, currencyDisplay)} (${usedItemized ? "itemized" : "standard"})`
    );
  }
  return parts.join("; ");
}

export function propertyPurchaseDetail(event, currencyDisplay) {
  const mortgage = Number(event.mortgagePrincipalUsd);
  const parts = [`down ${cu(event.downPaymentUsd, currencyDisplay)}`];
  if (mortgage > 0) parts.push(`mortgage ${cu(mortgage, currencyDisplay)}`);
  return parts.join("; ");
}

const dueWithShortfallDetail = (event, currencyDisplay) =>
  formatDueWithShortfall(event.amountDueUsd, event.shortfallUsd, currencyDisplay);

// A human-friendly name for the typed `AssetKey` an event carries — the display fallback when
// no curated `assetLabel` is set. Derived from the kind's own identifying field (crypto ticker,
// PE issuer, the S&P index name), not the old `crypto:btc`-style wire string.
function assetDisplayName(asset) {
  if (!asset) return undefined;
  switch (asset.kind) {
    case "crypto":
      return asset.symbol.toUpperCase();
    case "private_equity":
      return asset.issuerId;
    case "sp500":
      return "S&P 500";
    default:
      return undefined;
  }
}

// Single source of truth for per-event-kind label + detail rendering. Adding a new
// `RolloutEvent` discriminator must add an entry here, otherwise eventLabel/eventDetailText fall
// back to the generic "Event" / "" defaults.
export const EVENT_FORMATTERS = {
  holding_sale: {
    label: (event) => `Sold ${event.assetLabel ?? assetDisplayName(event.asset) ?? "asset"}`,
    detail: (event, currencyDisplay) =>
      `${fmtNumber(event.units)} units; basis ${cu(event.costBasisUsd, currencyDisplay)}`,
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
    label: (event) => `Accrued ${jurisdictionLabel(event.jurisdictionId)} tax`,
    detail: taxAccrualDetail,
  },
  tax_payment: { label: taxPaymentLabel, detail: dueWithShortfallDetail },
  property_purchase: { label: () => "Bought property", detail: propertyPurchaseDetail },
  closing_cost_payment: { label: () => "Paid closing costs", detail: () => "" },
  mortgage_payment: {
    label: () => "Paid mortgage",
    detail: (event, currencyDisplay) =>
      `interest ${cu(event.interestUsd, currencyDisplay)}; principal ${cu(event.principalUsd, currencyDisplay)}`,
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
    detail: (event, currencyDisplay) => `shortfall ${cu(event.shortfallUsd, currencyDisplay)}`,
  },
  set_rented_fraction: {
    label: (event) => {
      const fraction = Number(event.rentedFraction);
      if (fraction <= 0) return "Stopped renting";
      if (fraction >= 1) return "Started renting out fully";
      return `Set rented to ${(fraction * 100).toFixed(0)}%`;
    },
    detail: (event) => `${event.propertyId}`,
  },
  set_primary_residence: {
    label: (event) => (event.isPrimaryResidence ? "Set primary home" : "Cleared primary home"),
    detail: (event) => event.propertyId ?? event.agentId ?? "",
  },
  capital_improvement: {
    label: () => "Capital improvement",
    detail: (event, currencyDisplay) => `${event.propertyId}; basis bump ${cu(event.amountUsd, currencyDisplay)}`,
  },
  property_sale: {
    label: () => "Sold property",
    detail: (event, currencyDisplay) => {
      const parts = [
        `${event.propertyId}`,
        `proceeds ${cu(event.grossProceedsUsd, currencyDisplay)}`,
        `payoff ${cu(event.mortgagePayoffUsd, currencyDisplay)}`,
        `net cash ${cu(event.netCashToOwnerUsd, currencyDisplay)}`,
      ];
      const recapture = Number(event.depreciationRecaptureUsd);
      if (recapture > 0) parts.push(`§1250 ${cu(recapture, currencyDisplay)}`);
      const exclusion = Number(event.section121ExclusionUsd);
      if (exclusion > 0) parts.push(`§121 ${cu(exclusion, currencyDisplay)}`);
      const ltcg = Number(event.longTermCapitalGainUsd);
      if (ltcg > 0) parts.push(`LTCG ${cu(ltcg, currencyDisplay)}`);
      return parts.join("; ");
    },
  },
  private_equity_event: {
    label: (event) => {
      const label = event.assetLabel ?? assetDisplayName(event.asset) ?? "Private equity";
      if (event.eventKind === "tender") return `Tender: ${label}`;
      if (event.eventKind === "public_market_open") return `Public market: ${label}`;
      if (event.eventKind === "acquisition_cashout") return `Acquisition: ${label}`;
      if (event.eventKind === "legal_impairment") return `Liquidity impaired: ${label}`;
      if (event.eventKind === "forced_recovery") return `Recovery cashout: ${label}`;
      if (event.eventKind === "collapse") return `Collapsed: ${label}`;
      return `PE event: ${label}`;
    },
    detail: (event, currencyDisplay) => {
      const parts = [`mark ${cu(event.markUsd, currencyDisplay)}`, String(event.regime ?? "").replace(/_/g, " ")];
      const capacity = Number(event.saleCapacityFraction);
      if (Number.isFinite(capacity) && capacity < 1) parts.push(`capacity ${(capacity * 100).toFixed(0)}%`);
      const eligible = Number(event.eligibleFraction);
      if (Number.isFinite(eligible) && eligible < 1) parts.push(`eligible ${(eligible * 100).toFixed(0)}%`);
      const forcedSale = Number(event.forcedSaleFraction);
      if (forcedSale > 0) parts.push(`forced sale ${(forcedSale * 100).toFixed(0)}%`);
      if (event.liquidityBlocked) parts.push("liquidity blocked");
      const recovery = Number(event.forcedRecoveryCashoutUsd);
      if (recovery > 0) parts.push(`recovery ${cu(recovery, currencyDisplay)}`);
      return parts.filter(Boolean).join("; ");
    },
  },
  private_equity_opportunity: {
    label: (event) => {
      const label = event.assetLabel ?? assetDisplayName(event.asset) ?? "Private equity";
      const outcome = String(event.outcome ?? "").replace(/_/g, " ");
      return `PE opportunity: ${label}${outcome ? ` (${outcome})` : ""}`;
    },
    detail: (event, currencyDisplay) => {
      const parts = [
        `mark ${cu(event.markUsd, currencyDisplay)}`,
        `shortfall ${cu(event.shortfallUsd, currencyDisplay)}`,
        `target ${fmtNumber(event.targetUnits)} units`,
      ];
      const proceeds = Number(event.proceedsUsd);
      if (proceeds > 0) parts.push(`proceeds ${cu(proceeds, currencyDisplay)}`);
      const capacity = Number(event.saleCapacityFraction);
      if (Number.isFinite(capacity) && capacity < 1) parts.push(`capacity ${(capacity * 100).toFixed(0)}%`);
      const eligible = Number(event.eligibleFraction);
      if (Number.isFinite(eligible) && eligible < 1) parts.push(`eligible ${(eligible * 100).toFixed(0)}%`);
      if (event.liquidityBlocked) parts.push("liquidity blocked");
      return parts.filter(Boolean).join("; ");
    },
  },
};

export function eventLabel(event) {
  return EVENT_FORMATTERS[event?.kind]?.label(event) ?? "Event";
}

export function eventDetailText(event, currencyDisplay) {
  return EVENT_FORMATTERS[event?.kind]?.detail(event, currencyDisplay) ?? "";
}

export function eventTitle(event, currencyDisplay) {
  return `Month ${eventStateMonthIndex(event) ?? "n/a"}: ${eventLabel(event)} ${cu(eventAmount(event), currencyDisplay)}`;
}

export function portfolioHasBucket(portfolio, bucketName) {
  const holdings = portfolio?.holdings ?? [];
  if (bucketName === "crypto") {
    return holdings.some((position) => position.securityKind === "cryptocurrency");
  }
  // Match the backend sell-order compiler: private equity is handled by tender policies, not the
  // liquid "stocks" sale bucket.
  if (bucketName === "stocks") {
    return holdings.some((position) => isStockBucketPosition(position));
  }
  return false;
}

export function isPrivateSecurityPosition(position) {
  return position?.securityKind === "private_equity";
}

export function isStockBucketPosition(position) {
  return position != null && position.securityKind !== "cryptocurrency" && !isPrivateSecurityPosition(position);
}

export function firstSaleMonth(events) {
  let earliest = null;
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
export function isEventPostSale(event, saleMonth) {
  if (saleMonth == null) return false;
  if (event.month > saleMonth) return true;
  return event.month === saleMonth && event.kind !== "property_sale";
}
