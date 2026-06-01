import { clampInteger } from "./lib/format.ts";
import { rowsFrom } from "./lib/frame.ts";

// Sell-order is stored as a string of single-char bucket codes, in priority order. "pc" means
// "sell public securities first, then crypto if needed"; "c" means crypto only; "" disables auto
// liquidity sales entirely. The translation to the wire's `sell_order` tuple happens at scenario
// emission time. Storing it as a string (rather than an array) keeps default-comparison and URL
// encoding trivial.
export const SELL_BUCKETS = [
  { name: "stocks", code: "s", label: "Stocks" },
  { name: "crypto", code: "c", label: "Crypto" },
];
export const SELL_BUCKET_BY_CODE = new Map(SELL_BUCKETS.map((bucket) => [bucket.code, bucket]));
export const SELL_BUCKET_BY_NAME = new Map(SELL_BUCKETS.map((bucket) => [bucket.name, bucket]));
export const DEFAULT_SELL_ORDER_CODES = SELL_BUCKETS.map((bucket) => bucket.code).join("");

// Rollout count is NOT a product-input field: it is a top-level control shared across the
// product and calibration tabs (see `rolloutCountDefault` and the `?n=` URL param). Both tabs
// run this many rollouts, so it lives in the app shell rather than in either tab's input.
export const DEFAULT_ROLLOUT_COUNT = 500;
export const DEFAULT_FIRST_SEED = 1301;

export const DEFAULT_PRODUCT_INPUT_BASE = {
  horizonMonths: 48,
  monthlySpendUsd: 1400,
  spendIndex: "inflation",
  sellOrder: DEFAULT_SELL_ORDER_CODES,
  cashBufferTriggerBelowUsd: 4000,
  cashBufferSaleUsd: 10000,
  cashBufferIndexToInflation: true,
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
  // `{ kind, month, ...kind-specific }`. Persisted to the URL as a separate `lc` param
  // (a flat-positional `s=` packing can't represent variable-length structured lists).
  propertyLifecycleEvents: [],
};

export const LIFECYCLE_KINDS = [
  { value: "set_rented_fraction", label: "Change rented %" },
  { value: "set_primary_residence", label: "Set primary home" },
  { value: "capital_improvement", label: "Capital improvement" },
  { value: "property_sale", label: "Sell property" },
];
export const LIFECYCLE_KINDS_BY_VALUE = new Map(LIFECYCLE_KINDS.map((kind) => [kind.value, kind]));
export const LIFECYCLE_URL_KEY = "lc";
// Single-letter codes for the `?lc=` URL packing.
export const LIFECYCLE_KIND_CODES = {
  set_rented_fraction: "r",
  set_primary_residence: "p",
  capital_improvement: "c",
  property_sale: "s",
};
export const LIFECYCLE_KIND_FROM_CODE = {
  r: "set_rented_fraction",
  p: "set_primary_residence",
  c: "capital_improvement",
  s: "property_sale",
};

export const FAN_PERCENTILES = [5, 25, 50, 75, 95];

export const METRIC_OPTIONS = [
  { value: "net_worth_usd", chartValue: "netWorthUsd", label: "Net worth" },
  { value: "holding_value_usd", chartValue: "holdingValueUsd", label: "Holdings value" },
  { value: "private_equity_value_usd", chartValue: "privateEquityValueUsd", label: "Private equity value" },
  { value: "property_value_usd", chartValue: "propertyValueUsd", label: "Property value" },
  { value: "mortgage_balance_usd", chartValue: "mortgageBalanceUsd", label: "Mortgage balance" },
  { value: "home_equity_usd", chartValue: "homeEquityUsd", label: "Home equity" },
  { value: "liquid_net_worth_usd", chartValue: "liquidNetWorthUsd", label: "Liquid net worth" },
  { value: "cash_usd", chartValue: "cashUsd", label: "Cash balance" },
  { value: "shortfall_usd", chartValue: "shortfallUsd", label: "Cash shortfall" },
];

export const METRIC_BY_VALUE = new Map(METRIC_OPTIONS.map((metric) => [metric.value, metric]));

export function productInputDefaults(bootstrap) {
  // Server-provided overrides (from the deployment's augur YAML's `product_input_defaults`).
  // Each field is `null` when the deployment didn't set it; we drop those entries so the
  // frontend's hard-coded base value stays. The decamelize layer in `client.js` already
  // converted snake_case keys, so `cash_buffer_index_to_inflation` arrives as `cashBufferIndexToInflation`.
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

// Tab-shared first rollout seed. Product projections and calibration runs both consume the same
// seed sequence start, so the shell owns it and persists it to `?seed=`.
export function firstSeedDefault(bootstrap) {
  const override = bootstrap.productInputDefaults?.firstSeed;
  return clampFirstSeed(override ?? DEFAULT_FIRST_SEED);
}

export function clampFirstSeed(value) {
  return clampInteger(value, 0, 2 ** 31 - 1);
}

export function firstSeedFromSearch(searchString, bootstrap) {
  const raw = new URLSearchParams(searchString).get("seed");
  if (raw == null) return firstSeedDefault(bootstrap);
  const numeric = Number(raw);
  return Number.isFinite(numeric) ? clampFirstSeed(numeric) : firstSeedDefault(bootstrap);
}

// Tab-shared exogenous model preset. Like the rollout count, the app shell owns the live value
// and persists it to `?x=`; the product scenario and the calibration run both read it. A value
// not present in `bootstrap.exogenousPresets` (stale URL, empty) falls back to the deployment
// default. The selected id is always one of `bootstrap.exogenousPresets`.
export function exogenousModelDefault(bootstrap) {
  const presets = bootstrap.exogenousPresets ?? [];
  const preferred = bootstrap.defaultExogenousPresetId;
  return presets.includes(preferred) ? preferred : (presets[0] ?? null);
}

export function exogenousModelFromSearch(searchString, bootstrap) {
  const requested = new URLSearchParams(searchString).get("x");
  const presets = bootstrap.exogenousPresets ?? [];
  return requested && presets.includes(requested) ? requested : exogenousModelDefault(bootstrap);
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

// URL serialization: a single `?s=` query param carries all scenario inputs as a positional dot-
// separated string. A version letter prefix gates schema changes; trailing default values are
// trimmed; enums use one-letter codes. Examples:
//   ?s=4                                                  → all defaults
//   ?s=4.120..5000.n..200000.100000.....location_a_property..m.10
//
// The ordering, encoding, and code maps live here in INPUT_FIELDS. Adding a new input means
// appending to INPUT_FIELDS; old URLs continue to decode (missing positions = defaults).
// Bump SCHEMA_VERSION when a field's semantic encoding changes — old URLs then fall back to
// defaults rather than reinterpreting (e.g. v1 stored rentalFractionRented as a 0..1 fraction,
// v2 stores rentalFractionRentedPct as a 0..100 percentage, v3 dropped `rentItOut` so the
// rental fields are always present and `fractionRentedPct == 0` means "not rented", v4 dropped
// `rolloutCount` (now the tab-shared `?n=` control) and the trailing `modelId` (now the
// tab-shared `?x=` control). Dropping the trailing `modelId` slot shifts no other
// position, so v4 `?s=` strings that never set it decode identically.
// v5 dropped the leading `horizonMonths` (now the tab-shared `?h=` control); dropping a
// non-trailing slot shifts the rest, so the bump makes older URLs fall back to defaults rather
// than misread their positions.
// v6 dropped the leading `firstSeed` (now the tab-shared `?seed=` control); same positional
// shift concern as v5.
const INPUT_SCHEMA_VERSION = "6";

const INPUT_FIELDS = [
  { key: "monthlySpendUsd", type: "number" },
  { key: "spendIndex", type: "enum", codes: { inflation: "i", none: "n" } },
  // sellOrder is a string of single-char bucket codes; "" is a legitimate value meaning "disable
  // all auto-sales", so we use a sentinel ("_") in the URL to distinguish "explicitly empty"
  // from "default" (which the encoder also represents as "").
  { key: "sellOrder", type: "orderedCodes" },
  { key: "cashBufferTriggerBelowUsd", type: "number" },
  { key: "cashBufferSaleUsd", type: "number" },
  { key: "peLnwFloorUsd", type: "number" },
  { key: "peIndexFloorToInflation", type: "bool" },
  { key: "monthlyRentUsd", type: "number" },
  { key: "rentalLocationId", type: "string" },
  { key: "propertyId", type: "string" },
  { key: "livesHere", type: "bool" },
  { key: "financingKind", type: "enum", codes: { cash: "c", mortgage: "m" } },
  { key: "downPaymentPct", type: "number" },
  { key: "mortgageTermMonths", type: "number" },
  { key: "annualRatePct", type: "number" },
  { key: "annualInsurancePct", type: "number" },
  { key: "annualMaintenancePct", type: "number" },
  { key: "rentalFullPropertyMonthlyUsd", type: "number" },
  { key: "rentalFractionRentedPct", type: "number" },
  { key: "rentalVacancyPct", type: "number" },
  { key: "useRentalManagement", type: "bool" },
  { key: "managementFeePct", type: "number" },
  { key: "leasingFeeMonths", type: "number" },
  { key: "avgTenancyMonths", type: "number" },
  // Appended after existing positions so older `?s=` URLs continue to decode without shifting
  // their downstream slots. New optional fields go at the tail.
  { key: "cashBufferIndexToInflation", type: "bool" },
  { key: "modelId", type: "string" },
];

export function encodeInputValue(value, field) {
  if (value == null) return "";
  if (field.type === "bool") return value ? "1" : "0";
  if (field.type === "enum") {
    const code = field.codes[value];
    if (code == null) throw new Error(`unknown enum value ${value} for ${field.key}`);
    return code;
  }
  if (field.type === "string") return encodeURIComponent(String(value));
  if (field.type === "orderedCodes") return value === "" ? "_" : String(value);
  return String(value);
}

export function decodeInputValue(rawValue, field, defaultValue) {
  if (rawValue === "") return defaultValue;
  if (field.type === "bool") return rawValue === "1";
  if (field.type === "enum") {
    for (const [name, code] of Object.entries(field.codes)) {
      if (code === rawValue) return name;
    }
    return defaultValue;
  }
  if (field.type === "string") return decodeURIComponent(rawValue);
  if (field.type === "orderedCodes") return rawValue === "_" ? "" : rawValue;
  const numeric = Number(rawValue);
  return Number.isFinite(numeric) ? numeric : defaultValue;
}

export function productInputToSearch(input, bootstrap) {
  const defaults = productInputDefaults(bootstrap);
  const encoded = INPUT_FIELDS.map((field) => {
    if (input[field.key] === defaults[field.key]) return "";
    return encodeInputValue(input[field.key], field);
  });
  while (encoded.length > 0 && encoded[encoded.length - 1] === "") encoded.pop();
  const parts =
    encoded.length === 0 ? [`s=${INPUT_SCHEMA_VERSION}`] : [`s=${INPUT_SCHEMA_VERSION}.${encoded.join(".")}`];
  const lifecycle = lifecycleEventsToUrl(input.propertyLifecycleEvents);
  if (lifecycle) parts.push(`${LIFECYCLE_URL_KEY}=${lifecycle}`);
  return parts.join("&");
}

export function productInputFromSearch(searchString, bootstrap) {
  const defaults = productInputDefaults(bootstrap);
  const params = new URLSearchParams(searchString);
  const packed = params.get("s");
  const parsed = { ...defaults };
  if (packed) {
    const [version, ...values] = packed.split(".");
    if (version === INPUT_SCHEMA_VERSION) {
      values.forEach((rawValue, index) => {
        if (index >= INPUT_FIELDS.length) return;
        const field = INPUT_FIELDS[index];
        parsed[field.key] = decodeInputValue(rawValue, field, defaults[field.key]);
      });
    }
  }
  parsed.propertyLifecycleEvents = lifecycleEventsFromUrl(params.get(LIFECYCLE_URL_KEY));
  return parsed;
}

// `?lc=` packing: each event is `<kind-code><month>:<value>` joined by `~`. Examples:
//   r24:50  → set rented to 50% at month 24
//   p12:0  → clear primary-residence assignment at month 12
//   c12:50000  → $50k capex at month 12
//   s120:6  → sell at month 120 with 6% closing cost
export function lifecycleEventsToUrl(events) {
  if (!Array.isArray(events) || events.length === 0) return "";
  return events
    .map((event) => {
      const code = LIFECYCLE_KIND_CODES[event.kind];
      if (!code) return "";
      const month = Number(event.month) || 0;
      const value = lifecycleEventUrlValue(event);
      return `${code}${month}:${value}`;
    })
    .filter(Boolean)
    .join("~");
}

export function lifecycleEventUrlValue(event) {
  if (event.kind === "set_rented_fraction") return String(Math.round(Number(event.rentedFractionPct) || 0));
  if (event.kind === "set_primary_residence") return event.livesHere ? "1" : "0";
  if (event.kind === "capital_improvement") return String(Math.round(Number(event.amountUsd) || 0));
  if (event.kind === "property_sale") return String(Number(event.closingCostPct) || 0);
  return "";
}

export function lifecycleEventsFromUrl(packed) {
  if (!packed) return [];
  return packed
    .split("~")
    .map((entry) => parseLifecycleEntry(entry))
    .filter(Boolean);
}

export function parseLifecycleEntry(entry) {
  if (!entry || entry.length < 2) return null;
  const kind = LIFECYCLE_KIND_FROM_CODE[entry[0]];
  if (!kind) return null;
  const colonIdx = entry.indexOf(":");
  if (colonIdx < 0) return null;
  const month = Number(entry.slice(1, colonIdx));
  const raw = entry.slice(colonIdx + 1);
  if (!Number.isFinite(month) || month < 1) return null;
  const base = { _id: nextLifecycleEventId(), kind, month };
  if (kind === "set_rented_fraction") {
    const pct = Number(raw);
    if (!Number.isFinite(pct)) return null;
    return { ...base, rentedFractionPct: Math.min(100, Math.max(0, pct)) };
  }
  if (kind === "set_primary_residence") {
    return { ...base, livesHere: raw === "1" };
  }
  if (kind === "capital_improvement") {
    const amount = Number(raw);
    if (!Number.isFinite(amount) || amount <= 0) return null;
    return { ...base, amountUsd: amount };
  }
  if (kind === "property_sale") {
    const pct = Number(raw);
    if (!Number.isFinite(pct) || pct < 0 || pct > 100) return null;
    return { ...base, closingCostPct: pct };
  }
  return null;
}

// Stable per-event id used as the React key so editing/reordering preserves DOM identity
// (NumberInput focus state, mid-edit values). The id is purely UI state — `lifecycleEventsToUrl`
// reads only `kind`/`month`/value fields, so it isn't persisted in the URL. Events parsed back
// from the URL get a fresh id assigned in `lifecycleEventsFromUrl`.
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

export function sellOrderBuckets(sellOrderCodes) {
  const codes = String(sellOrderCodes ?? "");
  const buckets = [];
  for (const code of codes) {
    const bucket = SELL_BUCKET_BY_CODE.get(code);
    if (bucket && !buckets.includes(bucket.name)) buckets.push(bucket.name);
  }
  return buckets;
}

export function productScenario(input, bootstrap, modelId, horizonMonths) {
  const sellOrder = sellOrderBuckets(input.sellOrder);
  const autoSellEnabled = sellOrder.length > 0;
  const monthlyRentUsd = Math.max(0, Number(input.monthlyRentUsd) || 0);
  const rentalLocationId = monthlyRentUsd > 0 ? input.rentalLocationId : null;
  return {
    modelId: modelId || exogenousModelDefault(bootstrap),
    horizonMonths: clampHorizonMonths(horizonMonths, bootstrap),
    monthlySpendUsd: Math.max(1, Number(input.monthlySpendUsd) || 1),
    spendIndex: input.spendIndex === "none" ? "none" : "inflation",
    fundingPolicy: {
      cashBufferTriggerBelowUsd: autoSellEnabled ? Math.max(0, Number(input.cashBufferTriggerBelowUsd) || 0) : 0,
      cashBufferSaleUsd: autoSellEnabled ? Math.max(0, Number(input.cashBufferSaleUsd) || 0) : 0,
      cashBufferIndexToInflation: Boolean(input.cashBufferIndexToInflation),
      sellOrder,
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

export function productRolloutSeeds(bootstrap, rolloutCount, firstSeed) {
  const count = clampRolloutCount(rolloutCount, bootstrap);
  const start = clampFirstSeed(firstSeed);
  return Array.from({ length: count }, (_, index) => start + index);
}

// The tab-shared controls (rollout count, first seed, exogenous model, horizon) are passed in
// `shared` rather than read from `input`, since the app shell owns them
// (see `?n=`/`?seed=`/`?x=`/`?h=`).
export function productMetricFanRequest(input, bootstrap, metric, shared) {
  const { rolloutCount, firstSeed, exogenousModel, horizonMonths } = shared;
  return {
    scenario: productScenario(input, bootstrap, exogenousModel, horizonMonths),
    rolloutSeeds: productRolloutSeeds(bootstrap, rolloutCount, firstSeed),
    metric: metric.value,
    percentiles: FAN_PERCENTILES,
  };
}
