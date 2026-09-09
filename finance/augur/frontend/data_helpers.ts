import { rowsFrom } from "./lib/frame";
import {
  currencyQuantaAdd,
  currencyQuantaChartNumber,
  currencyQuantaCompare,
  currencyQuantaIsPositive,
  fmtQuanta,
  fmtNumber,
} from "./lib/format";
import { METRIC_OPTIONS } from "./input_helpers";
import { ROLLOUT_EVENT_KIND_ORDER, type RolloutEventKind } from "./rollout_event_vocabulary.generated";

export { ROLLOUT_EVENT_KIND_ORDER };

function currency(result) {
  return { currencyCode: result?.currencyCode, currencyQuantum: result?.currencyQuantum };
}

function cu(value, currencyMeta) {
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

export function metricFanRows(result) {
  if (!result?.monthlyMetricFan) return [];
  const byMonth = new Map();
  const displayValuesByMonth = new Map();
  for (const row of rowsFrom(result.monthlyMetricFan)) {
    if (row.valueQuanta == null) continue;
    const monthIndex = Number(row.monthIndex);
    const percentile = Number(row.percentile);
    const rawValue = row.valueQuanta;
    const metricValue = currencyQuantaChartNumber(rawValue, result.currencyQuantum);
    if (!Number.isFinite(monthIndex) || !Number.isFinite(percentile) || !Number.isFinite(metricValue)) continue;
    if (!byMonth.has(monthIndex)) byMonth.set(monthIndex, new Map());
    if (!displayValuesByMonth.has(monthIndex)) displayValuesByMonth.set(monthIndex, new Map());
    byMonth.get(monthIndex).set(percentile, metricValue);
    displayValuesByMonth.get(monthIndex).set(percentile, rawValue);
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
      return row.valueQuanta;
    }
  }
  return null;
}

export function terminalMetricSamples(result, metric) {
  if (result?.metric !== metric.value || !result?.terminalMetricSamples) return [];
  return rowsFrom(result.terminalMetricSamples)
    .filter((row) => row.valueQuanta != null)
    .map((row) => ({
      seed: Number(row.seed),
      value: currencyQuantaChartNumber(row.valueQuanta, result.currencyQuantum),
      currencyQuanta: row.valueQuanta,
      currency: currency(result),
      failed: Boolean(row.failed),
    }))
    .filter((row) => Number.isInteger(row.seed) && Number.isFinite(row.value));
}

export function terminalSampleAtPercentile(result, metric, percentile) {
  const samples = terminalMetricSamples(result, metric)
    .slice()
    .sort((left, right) => currencyQuantaCompare(left.currencyQuanta, right.currencyQuanta) || left.seed - right.seed);
  if (samples.length === 0) return null;
  if (samples.length === 1) return samples[0];
  const rank = Math.floor((Math.max(0, Math.min(100, Number(percentile))) / 100) * (samples.length - 1) + 0.5);
  return samples[Math.max(0, Math.min(samples.length - 1, rank))];
}

export function endingMetricValue(endingMetrics, metric) {
  return endingMetrics?.[metric.chartValue] ?? null;
}

const PROPERTY_METRIC_VALUES = new Set(["property_value"]);
const MORTGAGE_METRIC_VALUES = new Set(["mortgage_balance", "home_equity"]);

export function visibleMetricOptions(input) {
  const hasProperty = input?.propertyId != null;
  const hasMortgage = hasProperty && input?.financingKind === "mortgage";
  return METRIC_OPTIONS.filter((metric) => {
    if (!hasProperty && PROPERTY_METRIC_VALUES.has(metric.value)) return false;
    if (!hasMortgage && MORTGAGE_METRIC_VALUES.has(metric.value)) return false;
    return true;
  });
}

export function rolloutStatusText(summary) {
  if (!summary) return "No rollout selected";
  const failedMonth = summary.endingMetrics?.failedMonthIndex;
  if (summary.failed) return Number.isFinite(failedMonth) ? `failed m${failedMonth}` : "failed";
  return "completed";
}

export function selectedRolloutMetricRows(detail, metric) {
  if (!detail?.rollout?.monthlyMetrics) return [];
  const ending = detail.rollout.endingMetrics;
  return rowsFrom(detail.rollout.monthlyMetrics)
    .map((row) => ({
      monthIndex: Number(row.monthIndex),
      // The stopped post-event book has not observed the next month's market mark.
      year:
        (detail.rollout.failed && Number(row.monthIndex) === ending.snapshotIndex
          ? ending.failedMonthIndex
          : Number(row.monthIndex)) / 12,
      value: currencyQuantaChartNumber(row[metric.chartValue], detail.currencyQuantum),
      currencyQuanta: row[metric.chartValue],
      currency: currency(detail),
    }))
    .filter((row) => Number.isFinite(row.monthIndex) && Number.isFinite(row.value));
}

export function selectedRolloutEvents(detail) {
  return Array.isArray(detail?.rollout?.events)
    ? detail.rollout.events.map((event) => ({ ...event, _currency: currency(detail) }))
    : [];
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
  return event?.amountQuanta ?? null;
}

export function jurisdictionLabel(jurisdictionId) {
  if (jurisdictionId === "federal_us") return "federal";
  if (jurisdictionId === "california") return "California";
  return (jurisdictionId ?? "").replace(/_/g, " ");
}

export function formatDueWithShortfall(amountDueQuanta, shortfallQuanta, currencyMeta) {
  const base = `due ${cu(amountDueQuanta, currencyMeta)}`;
  return currencyQuantaIsPositive(shortfallQuanta) ? `${base}; shortfall ${cu(shortfallQuanta, currencyMeta)}` : base;
}

export function shortfallLabel(event, { ok, shortfall }) {
  return currencyQuantaIsPositive(event.shortfallQuanta) ? shortfall : ok;
}

export function taxPaymentLabel(event) {
  const isShortfall = currencyQuantaIsPositive(event.shortfallQuanta);
  if (event.obligationType === "estimated_tax") return isShortfall ? "Estimated tax shortfall" : "Paid estimated taxes";
  if (event.obligationType === "tax_true_up") return isShortfall ? "Tax true-up shortfall" : "Paid tax true-up";
  return isShortfall ? "Tax payment shortfall" : "Paid taxes";
}

export function taxAccrualDetail(event) {
  const gain = currencyQuantaAdd(event.ltcgQuanta, event.stcgQuanta);
  const parts = [
    `ordinary tax ${cu(event.ordinaryTaxQuanta, event._currency)}`,
    `gain tax ${cu(event.capitalGainTaxQuanta, event._currency)}`,
    `gains ${cu(gain, event._currency)}`,
  ];
  if (currencyQuantaIsPositive(event.mortgageInterestDeductionQuanta)) {
    const usedItemized =
      currencyQuantaIsPositive(event.itemizedDeductionQuanta) &&
      BigInt(event.itemizedDeductionQuanta) > BigInt(event.standardDeductionQuanta);
    parts.push(`MID ${cu(event.mortgageInterestDeductionQuanta, event._currency)}`);
    parts.push(
      `deduction ${cu(usedItemized ? event.itemizedDeductionQuanta : event.standardDeductionQuanta, event._currency)} (${usedItemized ? "itemized" : "standard"})`
    );
  }
  return parts.join("; ");
}

export function propertyPurchaseDetail(event) {
  const parts = [`down ${cu(event.downPaymentQuanta, event._currency)}`];
  if (currencyQuantaIsPositive(event.mortgagePrincipalQuanta)) {
    parts.push(`mortgage ${cu(event.mortgagePrincipalQuanta, event._currency)}`);
  }
  return parts.join("; ");
}

const dueWithShortfallDetail = (event) =>
  formatDueWithShortfall(event.amountDueQuanta, event.shortfallQuanta, event._currency);

// A human-friendly name for the typed `AssetKey` an event carries — the display fallback when
// no curated `assetLabel` is set. Derived from the kind's own identifying field (the security's
// symbol, the PE issuer), not the old `crypto:btc`-style wire string.
function assetDisplayName(asset) {
  if (!asset) return undefined;
  switch (asset.kind) {
    case "security":
      return asset.symbol.toUpperCase();
    case "private_equity":
      return asset.issuerId;
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
    detail: (event) => `${fmtNumber(event.units)} units; basis ${cu(event.costBasisQuanta, event._currency)}`,
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
    detail: (event) =>
      `interest ${cu(event.interestQuanta, event._currency)}; principal ${cu(event.principalQuanta, event._currency)}`,
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
    detail: (event) => `shortfall ${cu(event.shortfallQuanta, event._currency)}`,
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
    detail: (event) => `${event.propertyId}; basis bump ${cu(event.amountQuanta, event._currency)}`,
  },
  property_sale: {
    label: () => "Sold property",
    detail: (event) => {
      const parts = [
        `${event.propertyId}`,
        `proceeds ${cu(event.grossProceedsQuanta, event._currency)}`,
        `payoff ${cu(event.mortgagePayoffQuanta, event._currency)}`,
        `net cash ${cu(event.netCashToOwnerQuanta, event._currency)}`,
      ];
      if (currencyQuantaIsPositive(event.depreciationRecaptureQuanta)) {
        parts.push(`§1250 ${cu(event.depreciationRecaptureQuanta, event._currency)}`);
      }
      if (currencyQuantaIsPositive(event.section121ExclusionQuanta)) {
        parts.push(`§121 ${cu(event.section121ExclusionQuanta, event._currency)}`);
      }
      if (currencyQuantaIsPositive(event.longTermCapitalGainQuanta)) {
        parts.push(`LTCG ${cu(event.longTermCapitalGainQuanta, event._currency)}`);
      }
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
    detail: (event) => {
      const parts = [`mark ${cu(event.markQuanta, event._currency)}`, String(event.regime ?? "").replace(/_/g, " ")];
      const capacity = Number(event.saleCapacityFraction);
      if (Number.isFinite(capacity) && capacity < 1) parts.push(`capacity ${(capacity * 100).toFixed(0)}%`);
      const eligible = Number(event.eligibleFraction);
      if (Number.isFinite(eligible) && eligible < 1) parts.push(`eligible ${(eligible * 100).toFixed(0)}%`);
      const forcedSale = Number(event.forcedSaleFraction);
      if (forcedSale > 0) parts.push(`forced sale ${(forcedSale * 100).toFixed(0)}%`);
      if (event.liquidityBlocked) parts.push("liquidity blocked");
      if (currencyQuantaIsPositive(event.forcedRecoveryCashoutQuanta)) {
        parts.push(`recovery ${cu(event.forcedRecoveryCashoutQuanta, event._currency)}`);
      }
      return parts.filter(Boolean).join("; ");
    },
  },
  private_equity_opportunity: {
    label: (event) => {
      const label = event.assetLabel ?? assetDisplayName(event.asset) ?? "Private equity";
      const outcome = String(event.outcome ?? "").replace(/_/g, " ");
      return `PE opportunity: ${label}${outcome ? ` (${outcome})` : ""}`;
    },
    detail: (event) => {
      const parts = [
        `mark ${cu(event.markQuanta, event._currency)}`,
        `shortfall ${cu(event.shortfallQuanta, event._currency)}`,
        `target ${fmtNumber(event.targetUnits)} units`,
      ];
      if (currencyQuantaIsPositive(event.proceedsQuanta)) {
        parts.push(`proceeds ${cu(event.proceedsQuanta, event._currency)}`);
      }
      const capacity = Number(event.saleCapacityFraction);
      if (Number.isFinite(capacity) && capacity < 1) parts.push(`capacity ${(capacity * 100).toFixed(0)}%`);
      const eligible = Number(event.eligibleFraction);
      if (Number.isFinite(eligible) && eligible < 1) parts.push(`eligible ${(eligible * 100).toFixed(0)}%`);
      if (event.liquidityBlocked) parts.push("liquidity blocked");
      return parts.filter(Boolean).join("; ");
    },
  },
} satisfies Record<RolloutEventKind, unknown>;

export function eventLabel(event) {
  return EVENT_FORMATTERS[event?.kind]?.label(event) ?? "Event";
}

export function eventDetailText(event) {
  return EVENT_FORMATTERS[event?.kind]?.detail(event) ?? "";
}

export function eventTitle(event) {
  return `Month ${eventStateMonthIndex(event) ?? "n/a"}: ${eventLabel(event)} ${cu(eventAmount(event), event?._currency)}`;
}

// Rows the target allocation may name, in portfolio order, keyed by the SERIES symbol the sim
// acts on — not the holding's display ticker, which can differ (a VOO holding is priced by the
// SPY series) and which the backend would then fail to match, silently disabling auto-sale. Two
// holdings sharing one series collapse into a single row, because the sim cannot tell them apart
// either — so their values sum, which is what the weight seed has to divide by.
// Private equity is absent because its key carries no symbol: it leaves only via a tender event.
export function sellableSecurities(portfolio) {
  const bySymbol = new Map();
  for (const position of portfolio?.holdings ?? []) {
    const symbol = isPrivateSecurityPosition(position) ? null : position.asset?.symbol;
    if (!symbol) continue;
    const label = position.label || position.symbol || symbol;
    const valueQuanta = position.currentValueQuanta ?? "0";
    const row = bySymbol.get(symbol);
    if (row) {
      row.labels.push(label);
      row.valueQuanta = currencyQuantaAdd(row.valueQuanta, valueQuanta);
    } else bySymbol.set(symbol, { symbol, labels: [label], valueQuanta });
  }
  return [...bySymbol.values()].map(({ symbol, labels, valueQuanta }) => ({
    symbol,
    label: labels.join(" + "),
    valueQuanta,
  }));
}

// Reads the typed asset key, not a display string: private equity is a different KIND of
// holding, and `securityKind` now describes only how to present a tradable security.
export function isPrivateSecurityPosition(position) {
  return position?.asset?.kind === "private_equity";
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
