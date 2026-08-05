import { clampInteger } from "./lib/format";

// Sell-order is stored as a comma-joined list of security symbols, in priority order.
// "VOO,BTC" means "sell VOO first, then BTC if needed"; "" disables auto liquidity sales
// entirely; `null` (the default) means "any sellable holding". Storing it as a string rather
// than an array keeps default-comparison and URL encoding trivial. The wire takes the same
// symbols — there is no bucket vocabulary in between.

// Rollout count is NOT a product-input field: it is a top-level control shared across the
// product and calibration tabs (see `rolloutCountDefault` and the `?n=` URL param). Both tabs
// run this many rollouts, so it lives in the app shell rather than in either tab's input.
export const DEFAULT_ROLLOUT_COUNT = 500;
export const DEFAULT_FIRST_SEED = 1;

export const DEFAULT_PRODUCT_INPUT_BASE = {
  horizonMonths: 48,
  monthlySpendUsd: 1400,
  spendIndex: "inflation",
  sleeveWeights: null,
  cashFloorUsd: 4000,
  cashCeilingUsd: 10000,
  cashBandIndexToInflation: true,
  peLnwFloorUsd: 0,
  peIndexFloorToInflation: true,
  monthlyRentUsd: 0,
  rentalLocationId: null,
  propertyId: null,
  livesHere: true,
  financingKind: "cash",
  downPaymentPct: 20,
  mortgageTermMonths: 360,
  annualRatePct: 7,
  annualInsurancePct: 0.4,
  annualMaintenancePct: 1.0,
  // Full-property monthly rent override before rented-fraction and vacancy scaling:
  // null means "use the property's rent_estimate_usd"; 0 means "no rent collected";
  // any positive value overrides the property record.
  rentalFullPropertyMonthlyUsd: null,
  // 0 = not rented (pure primary residence); 100 = fully rented out; in between = partial
  // (e.g. owner-occupied + ADU). Drives whether `initial_rental` is emitted on the wire.
  rentalFractionRentedPct: 0,
  rentalVacancyPct: 5,
  useRentalManagement: false,
  managementFeePct: 8,
  leasingFeeMonths: 1.0,
  avgTenancyMonths: 24,
  // Mid-horizon lifecycle events for the purchased property. Each row is
  // `{ kind, month, ...kind-specific }`, carried inside the `?scenarios=` JSON blob.
  propertyLifecycleEvents: [],
};

export const LIFECYCLE_KINDS = [
  { value: "set_rented_fraction", label: "Change rented %" },
  { value: "set_primary_residence", label: "Set primary home" },
  { value: "capital_improvement", label: "Capital improvement" },
  { value: "property_sale", label: "Sell property" },
];
export const LIFECYCLE_KINDS_BY_VALUE = new Map(LIFECYCLE_KINDS.map((kind) => [kind.value, kind]));

export const FAN_PERCENTILES = [5, 25, 50, 75, 95];
export const TERMINAL_DISTRIBUTION_PERCENTILES = Array.from({ length: 101 }, (_value, percentile) => percentile);

// Ordered as distinct buckets first, then the sums that aggregate them: the liquid buckets
// (public-equity holdings, private equity, cash) feed Liquid net worth; the property buckets
// (value, mortgage, home equity) plus liquid net worth feed Net worth. Cash shortfall is a
// diagnostic, kept last. This order drives the metric picker and both terminal tables.
export const METRIC_OPTIONS = [
  { value: "holding_value_usd", chartValue: "holdingValueUsd", label: "Holdings value" },
  { value: "private_equity_value_usd", chartValue: "privateEquityValueUsd", label: "Private equity value" },
  { value: "cash_usd", chartValue: "cashUsd", label: "Cash balance" },
  { value: "liquid_net_worth_usd", chartValue: "liquidNetWorthUsd", label: "Liquid net worth" },
  { value: "property_value_usd", chartValue: "propertyValueUsd", label: "Property value" },
  { value: "mortgage_balance_usd", chartValue: "mortgageBalanceUsd", label: "Mortgage balance" },
  { value: "home_equity_usd", chartValue: "homeEquityUsd", label: "Home equity" },
  { value: "net_worth_usd", chartValue: "netWorthUsd", label: "Net worth" },
];

export const METRIC_BY_VALUE = new Map(METRIC_OPTIONS.map((metric) => [metric.value, metric]));

export function productInputDefaults(bootstrap) {
  // Server-provided overrides (from the deployment's augur YAML's `product_input_defaults`).
  // Each field is `null` when the deployment didn't set it; we drop those entries so the
  // frontend's hard-coded base value stays. The decamelize layer in `client.js` already
  // converted snake_case keys, so `cash_band_index_to_inflation` arrives as `cashBandIndexToInflation`.
  const overrides = bootstrap.productInputDefaults ?? {};
  const overridesNotNull = Object.fromEntries(Object.entries(overrides).filter(([, value]) => value != null));
  const base = { ...DEFAULT_PRODUCT_INPUT_BASE, ...overridesNotNull };
  // Per-bootstrap derived clamps + fallbacks. These take effect after both base and YAML so we
  // only fall back to the first location when YAML didn't pin a `rental_location_id`. `horizonMonths`
  // is NOT part of the input object — it's the tab-shared `?h=` control (see `horizonMonthsDefault`).
  const { horizonMonths: _horizonMonths, ...baseWithoutHorizon } = base;
  return {
    ...baseWithoutHorizon,
    rentalLocationId: base.rentalLocationId ?? bootstrap.locations[0]?.id ?? null,
  };
}

// Tab-shared rollout count. The app shell owns the live value and persists it to `?n=`; the
// product and calibration workspaces both read it. The default honors a deployment's
// `product_input_defaults.rollout_count` override and is clamped to `max_rollout_samples`.
export function rolloutCountDefault(bootstrap) {
  const override = bootstrap.productInputDefaults?.rolloutCount;
  return clampInteger(override ?? DEFAULT_ROLLOUT_COUNT, 1, bootstrap.maxRolloutSamples);
}

export function clampRolloutCount(value, bootstrap) {
  return clampInteger(value, 1, bootstrap.maxRolloutSamples);
}

export function rolloutCountFromSearch(searchString, bootstrap) {
  const raw = new URLSearchParams(searchString).get("n");
  if (raw == null) return rolloutCountDefault(bootstrap);
  const numeric = Number(raw);
  return Number.isFinite(numeric) ? clampRolloutCount(numeric, bootstrap) : rolloutCountDefault(bootstrap);
}

// First rollout seed. Product projections and calibration runs consume the same seed-sequence
// start; it's a fixed default (no longer user-tunable) so identical seeds reproduce identical
// sampled exogenous paths across runs.
export function firstSeedDefault(bootstrap) {
  const override = bootstrap.productInputDefaults?.firstSeed;
  return clampFirstSeed(override ?? DEFAULT_FIRST_SEED);
}

export function clampFirstSeed(value) {
  return clampInteger(value, 0, 2 ** 31 - 1);
}

// Tab-shared exogenous model preset. Like the rollout count, the app shell owns the live value
// and persists it to `?x=`; the product scenario and the calibration run both read it. A value
// not in `bootstrap.models` (stale URL, empty) falls back to the deployment default. The
// selected id is always one of `bootstrap.models`.
export function defaultModel(bootstrap) {
  const preferred = bootstrap.defaultModelId;
  return bootstrap.models.includes(preferred) ? preferred : bootstrap.models[0];
}

export function modelFromSearch(searchString, bootstrap) {
  const requested = new URLSearchParams(searchString).get("x");
  return requested && bootstrap.models.includes(requested) ? requested : defaultModel(bootstrap);
}

// Tab-shared horizon (months). Like the rollout count, the app shell owns the live value and
// persists it to `?h=`; the product scenario and the calibration run both read it. The default
// honors a deployment's `product_input_defaults.horizon_months` override, clamped to
// `max_horizon_months`.
export function horizonMonthsDefault(bootstrap) {
  const override = bootstrap.productInputDefaults?.horizonMonths;
  return clampInteger(override ?? DEFAULT_PRODUCT_INPUT_BASE.horizonMonths, 1, bootstrap.maxHorizonMonths);
}

export function clampHorizonMonths(value, bootstrap) {
  return clampInteger(value, 1, bootstrap.maxHorizonMonths);
}

export function horizonMonthsFromSearch(searchString, bootstrap) {
  const raw = new URLSearchParams(searchString).get("h");
  if (raw == null) return horizonMonthsDefault(bootstrap);
  const numeric = Number(raw);
  return Number.isFinite(numeric) ? clampHorizonMonths(numeric, bootstrap) : horizonMonthsDefault(bootstrap);
}

// Tab-shared chart scale (linear/log), owned by the app shell and persisted to `?scale=`. Both
// the product metric fan and the calibration mark fan honor it. Default is linear.
export function metricScaleFromSearch(searchString) {
  return new URLSearchParams(searchString).get("scale") === "log" ? "log" : "linear";
}

// Stable per-event id used as the React key so editing/reordering preserves DOM identity
// (NumberInput focus state, mid-edit values). The id is purely UI state — `stripLifecycleIds`
// drops it before the `?scenarios=` blob is serialized, and `regenerateLifecycleIds` mints a fresh
// one on decode so base and variants never share a (colliding) key.
let _nextLifecycleEventId = 0;
export function nextLifecycleEventId() {
  _nextLifecycleEventId += 1;
  return `lc-${_nextLifecycleEventId}`;
}

export function defaultLifecycleEvent(kind, suggestedMonth) {
  const base = { _id: nextLifecycleEventId(), kind, month: Math.max(1, suggestedMonth || 12) };
  if (kind === "set_rented_fraction") return { ...base, rentedFractionPct: 0 };
  if (kind === "set_primary_residence") return { ...base, livesHere: false };
  if (kind === "capital_improvement") return { ...base, amountUsd: 25000 };
  if (kind === "property_sale") return { ...base, closingCostPct: 6 };
  throw new Error(`unknown lifecycle event kind ${kind}`);
}

export function buildPropertyFinancing(input) {
  if (input.financingKind !== "mortgage") return { kind: "cash" };
  return {
    kind: "mortgage",
    termMonths: Number(input.mortgageTermMonths) === 180 ? 180 : 360,
    downPaymentPct: Math.max(0, Number(input.downPaymentPct) || 0),
    annualRatePct: Math.max(0, Number(input.annualRatePct) || 0),
  };
}

export function buildRentalIncomePlan(input) {
  // `rentalFractionRentedPct` = 0 → property isn't rented at all; no rental plan on the wire.
  const fractionPct = Number(input.rentalFractionRentedPct) || 0;
  if (fractionPct <= 0) return null;
  // `rentalFullPropertyMonthlyUsd` is null → use property default; numeric → explicit override (incl. 0).
  const override = input.rentalFullPropertyMonthlyUsd;
  const fullPropertyMonthlyRentUsd = override == null ? null : Math.max(0, Number(override) || 0);
  const fraction = Math.min(1, Math.max(0.01, fractionPct / 100));
  const vacancyPct = Math.min(1, Math.max(0, (Number(input.rentalVacancyPct) || 0) / 100));
  return { fullPropertyMonthlyRentUsd, fractionRented: fraction, vacancyPct };
}

export function buildRentalManagement(input) {
  // Management agency only makes sense when there's something to rent.
  if (!input.useRentalManagement || (Number(input.rentalFractionRentedPct) || 0) <= 0) return null;
  return {
    managementFeePct: Math.max(0, Number(input.managementFeePct) || 0),
    leasingFeeMonths: Math.max(0, Number(input.leasingFeeMonths) || 0),
    avgTenancyMonths: clampInteger(input.avgTenancyMonths, 1, 600),
  };
}

export function buildPropertyPurchase(input) {
  if (!input.propertyId) return null;
  const initialRental = buildRentalIncomePlan(input);
  // Wire enforces is_primary_residence=False when fraction_rented=1.0; mirror that here
  // so the user can't submit an inconsistent ScenarioKey from the UI. The check is on the
  // normalized 0..1 fraction the builder just emitted (UI keeps the percentage form).
  const isPrimaryResidence = initialRental?.fractionRented === 1.0 ? false : Boolean(input.livesHere);
  return {
    propertyId: input.propertyId,
    financing: buildPropertyFinancing(input),
    isPrimaryResidence,
    initialRental,
    rentalManagement: buildRentalManagement(input),
    lifecycleEvents: buildLifecycleEvents(input.propertyLifecycleEvents),
  };
}

export function buildLifecycleEvents(events) {
  if (!Array.isArray(events) || events.length === 0) return [];
  return events
    .slice()
    .sort((a, b) => a.month - b.month)
    .map((event) => {
      if (event.kind === "set_rented_fraction") {
        return {
          kind: "set_rented_fraction",
          month: event.month,
          rentedFraction: (Number(event.rentedFractionPct) || 0) / 100,
        };
      }
      if (event.kind === "set_primary_residence") {
        return {
          kind: "set_primary_residence",
          month: event.month,
          isPrimaryResidence: Boolean(event.livesHere),
        };
      }
      if (event.kind === "capital_improvement") {
        return {
          kind: "capital_improvement",
          month: event.month,
          amountUsd: Number(event.amountUsd) || 0,
          description: "",
        };
      }
      if (event.kind === "property_sale") {
        return { kind: "property_sale", month: event.month, closingCostPct: Number(event.closingCostPct) || 0 };
      }
      throw new Error(`unknown lifecycle event kind ${event.kind}`);
    });
}

// Weights are seeded from what the owner currently HOLDS: each sellable position's share of
// total holding value, as an integer out of 100. Opening the editor and changing nothing then
// means "hold what you have" — the target matches today's portfolio, so the first sale does not
// silently rebalance a 90/10 split to 50/50.
//
// The seed lives here rather than in the backend lowering because the current VALUE of each
// holding is on the wire the frontend already has; the lowering sees only cost basis, and cost
// is not value.
export function seedSleeveWeights(holdings) {
  const sellable = (holdings ?? []).filter((h) => h.symbol != null && Number(h.holdingValueUsd) > 0);
  const total = sellable.reduce((sum, h) => sum + Number(h.holdingValueUsd), 0);
  if (!total) return [];
  return sellable.map((h) => ({
    symbol: h.symbol,
    weight: Math.max(1, Math.round((100 * Number(h.holdingValueUsd)) / total)),
  }));
}

// `null` means "not edited yet" and seeds from the portfolio; an explicit list is taken as-is,
// including the empty list, which is how auto-sale is turned off.
export function resolveSleeveWeights(sleeveWeights, holdings) {
  if (sleeveWeights == null) return seedSleeveWeights(holdings);
  return sleeveWeights
    .filter((sleeve) => sleeve.symbol)
    .map((sleeve) => ({ symbol: sleeve.symbol, weight: Math.max(0, Math.trunc(Number(sleeve.weight) || 0)) }));
}

export function productScenario(input, bootstrap, modelId, horizonMonths, holdings) {
  const sleeveWeights = resolveSleeveWeights(input.sleeveWeights, holdings);
  const monthlyRentUsd = Math.max(0, Number(input.monthlyRentUsd) || 0);
  const rentalLocationId = monthlyRentUsd > 0 ? input.rentalLocationId : null;
  return {
    modelId: modelId || defaultModel(bootstrap),
    horizonMonths: clampHorizonMonths(horizonMonths, bootstrap),
    monthlySpendUsd: Math.max(1, Number(input.monthlySpendUsd) || 1),
    spendIndex: input.spendIndex === "none" ? "none" : "inflation",
    fundingPolicy: {
      cashFloorUsd: Math.max(0, Number(input.cashFloorUsd) || 0),
      cashCeilingUsd: Math.max(
        Math.max(0, Number(input.cashFloorUsd) || 0),
        Math.max(0, Number(input.cashCeilingUsd) || 0)
      ),
      cashBandIndexToInflation: Boolean(input.cashBandIndexToInflation),
      sleeveWeights,
    },
    peTenderPolicy: {
      liquidNetWorthFloorUsd: Math.max(0, Number(input.peLnwFloorUsd) || 0),
      indexFloorToInflation: Boolean(input.peIndexFloorToInflation),
    },
    monthlyRentUsd,
    rentalLocationId,
    propertyPurchase: buildPropertyPurchase(input),
    annualInsurancePct: Math.max(0, Number(input.annualInsurancePct) || 0),
    annualMaintenancePct: Math.max(0, Number(input.annualMaintenancePct) || 0),
  };
}

// The tab-shared controls (rollout count, exogenous model, horizon — plus the fixed first seed) are
// passed in `shared` rather than read from `input`, since the app shell owns them
// (see `?n=`/`?x=`/`?h=`).
export function productMetricFanRequest(input, bootstrap, metric, shared) {
  const { rolloutCount, firstSeed, model, horizonMonths } = shared;
  return {
    scenario: productScenario(input, bootstrap, model, horizonMonths),
    firstSeed: clampFirstSeed(firstSeed),
    rolloutCount: clampRolloutCount(rolloutCount, bootstrap),
    metric: metric.value,
    percentiles: FAN_PERCENTILES,
  };
}

export function productTerminalDistributionRequest(input, bootstrap, metric, shared) {
  const { rolloutCount, firstSeed, model, horizonMonths } = shared;
  return {
    scenario: productScenario(input, bootstrap, model, horizonMonths),
    firstSeed: clampFirstSeed(firstSeed),
    rolloutCount: clampRolloutCount(rolloutCount, bootstrap),
    metric: metric.value,
    percentiles: TERMINAL_DISTRIBUTION_PERCENTILES,
  };
}

// -- Scenario set (multi-scenario comparison) ---------------------------------
//
// The product view can hold a *set* of scenarios that share one rollout seed window (and
// thus one sampled exogenous bundle: identical seeds reproduce identical market paths, so
// the comparison is apples-to-apples without any backend change). Each entry carries its own
// `ProductInput`; the chart overlays one median/P5/P95 fan per scenario, one color each.
//
// One scenario is "active": its rollout histogram, selected-rollout overlay, events, and the
// detailed terminal-percentile table scope to it. Color is assigned by position so it stays
// stable as the active selection changes; the active scenario is distinguished by line weight,
// not by hue.

// Per-scenario fan colors. Index 0 is the Base series (the existing single-scenario blue); variants
// take the rest. Deliberately red-free: red (`FAILED_ROLLOUT_COLOR`) is reserved for failed
// rollouts, so a red line never reads as a scenario hue. Blue / teal / violet / gold / cyan.
export const SCENARIO_COLORS = ["#2563eb", "#0d9488", "#7c3aed", "#ca8a04", "#0e7490"];
// Base is series 0; variants share the remaining colors.
export const MAX_VARIANTS = SCENARIO_COLORS.length - 1;

export function scenarioColor(index) {
  const count = SCENARIO_COLORS.length;
  return SCENARIO_COLORS[((index % count) + count) % count];
}

export function defaultVariantLabel(index) {
  return `Variant ${index + 1}`;
}

let _nextVariantId = 0;
// Stable per-variant id used as the React key + the active-selection handle. UI-only — the URL
// encodes variants positionally, so ids are minted fresh on every decode.
export function nextVariantId() {
  _nextVariantId += 1;
  return `var-${_nextVariantId}`;
}

export function makeVariant(label, overrides = {}) {
  return { id: nextVariantId(), label, overrides };
}

// A variant resolves against the base: its overrides win on the keys it sets.
export function resolveVariant(baseInput, overrides) {
  return { ...baseInput, ...overrides };
}

function regenerateLifecycleIds(events) {
  return Array.isArray(events) ? events.map((event) => ({ ...event, _id: nextLifecycleEventId() })) : [];
}

// Lifecycle-event `_id`s are UI-only React keys; drop them from anything we persist so two scenarios
// that share a lifecycle plan don't also share (colliding) keys after a decode.
function stripLifecycleIds(obj) {
  if (!obj || !("propertyLifecycleEvents" in obj)) return obj;
  const events = Array.isArray(obj.propertyLifecycleEvents) ? obj.propertyLifecycleEvents : [];
  return { ...obj, propertyLifecycleEvents: events.map(({ _id, ...rest }) => rest) };
}

function deserializeBaseInput(raw, defaults) {
  const merged = { ...defaults, ...(raw && typeof raw === "object" ? raw : {}) };
  merged.propertyLifecycleEvents = regenerateLifecycleIds(raw?.propertyLifecycleEvents);
  return merged;
}

function deserializeOverrides(raw) {
  if (!raw || typeof raw !== "object") return {};
  const overrides = { ...raw };
  if ("propertyLifecycleEvents" in overrides) {
    overrides.propertyLifecycleEvents = regenerateLifecycleIds(raw.propertyLifecycleEvents);
  }
  return overrides;
}

// Bump when the `?scenarios=` payload shape changes; an unrecognized version falls back to a
// default base-only set rather than misreading fields. v2 = base input + per-variant override diffs.
const SCENARIO_SET_VERSION = 2;

function decodeScenarioSet(packed, bootstrap) {
  let payload;
  try {
    payload = JSON.parse(packed);
  } catch (error) {
    // A hand-edited / truncated link is external input, not a bug: fall back to a default set.
    if (error instanceof SyntaxError) return null;
    throw error;
  }
  if (payload?.v !== SCENARIO_SET_VERSION || !payload.base) return null;
  const defaults = productInputDefaults(bootstrap);
  const base = {
    label: typeof payload.base.label === "string" && payload.base.label !== "" ? payload.base.label : "Base",
    input: deserializeBaseInput(payload.base.input, defaults),
  };
  const variants = (Array.isArray(payload.variants) ? payload.variants : [])
    .slice(0, MAX_VARIANTS)
    .map((raw, index) =>
      makeVariant(
        typeof raw?.label === "string" && raw.label !== "" ? raw.label : defaultVariantLabel(index),
        deserializeOverrides(raw?.overrides)
      )
    );
  return { base, variants, activeId: "base" };
}

// Encode the set to a single `?scenarios=` JSON param (`v: 2`): the Base full input plus per-variant
// override diffs. A lone Base (variants: []) uses the same form — one URL format for every set.
export function scenarioSetToSearch(base, variants) {
  const payload = {
    v: SCENARIO_SET_VERSION,
    base: { label: base.label, input: stripLifecycleIds(base.input) },
    variants: variants.map((variant) => ({ label: variant.label, overrides: stripLifecycleIds(variant.overrides) })),
  };
  const params = new URLSearchParams();
  params.set("scenarios", JSON.stringify(payload));
  return params.toString();
}

export function scenarioSetFromSearch(searchString, bootstrap) {
  const packed = new URLSearchParams(searchString).get("scenarios");
  if (packed != null) {
    const decoded = decodeScenarioSet(packed, bootstrap);
    if (decoded) return decoded;
  }
  // No `?scenarios=` (or a malformed/unrecognized blob) → a default Base with no variants.
  return {
    base: { label: "Base", input: productInputDefaults(bootstrap) },
    variants: [],
    activeId: "base",
  };
}
