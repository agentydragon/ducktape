// Unit tests for the scenario-set URL codec (Base + per-variant overrides) and for the target
// allocation the funding policy carries. Runs under vitest; see //augur/frontend:input_helpers_test.

import { test, expect } from "vitest";

import {
  scenarioSetToSearch,
  scenarioSetFromSearch,
  makeVariant,
  resolveVariant,
  productInputDefaults,
  productScenario,
  resolveSleeveWeights,
  MAX_VARIANTS,
} from "./input_helpers";

// `productInputDefaults` only reads `productInputDefaults` (deployment overrides) and
// `locations[0]?.id`; an empty bootstrap exercises the pure hard-coded defaults.
const bootstrap = { productInputDefaults: {}, locations: [] };

function baseWith(overrides, label = "Base") {
  return { label, input: { ...productInputDefaults(bootstrap), ...overrides } };
}

test("a base with no variants encodes as ?scenarios= and round-trips", () => {
  const search = scenarioSetToSearch(baseWith({ monthlySpendUsd: 4200 }), []);
  expect(search).toContain("scenarios=");

  const decoded = scenarioSetFromSearch(search, bootstrap);
  expect(decoded.variants).toHaveLength(0);
  expect(decoded.base.input.monthlySpendUsd).toBe(4200);
  expect(decoded.activeId).toBe("base");
});

test("a URL with no ?scenarios= decodes to a default base with no variants", () => {
  const decoded = scenarioSetFromSearch("", bootstrap);
  expect(decoded.variants).toHaveLength(0);
  expect(decoded.base.label).toBe("Base");
  expect(decoded.base.input.monthlySpendUsd).toBe(productInputDefaults(bootstrap).monthlySpendUsd);
  expect(decoded.activeId).toBe("base");
});

test("base + variants encode as ?scenarios= and round-trip base input, labels, and overrides", () => {
  const base = baseWith({ monthlyRentUsd: 3000 }, "Rent");
  const variants = [makeVariant("Mortgage", { financingKind: "mortgage", monthlySpendUsd: 5000 })];
  const search = scenarioSetToSearch(base, variants);
  expect(search).toContain("scenarios=");
  expect(search).not.toMatch(/(^|&)s=/);

  const decoded = scenarioSetFromSearch(search, bootstrap);
  expect(decoded.base.label).toBe("Rent");
  expect(decoded.base.input.monthlyRentUsd).toBe(3000);
  expect(decoded.variants).toHaveLength(1);
  expect(decoded.variants[0].label).toBe("Mortgage");
  expect(decoded.variants[0].overrides).toMatchObject({ financingKind: "mortgage", monthlySpendUsd: 5000 });
  // The variant inherits the base's rent (not overridden) and applies only its own overrides.
  const resolved = resolveVariant(decoded.base.input, decoded.variants[0].overrides);
  expect(resolved.monthlyRentUsd).toBe(3000);
  expect(resolved.financingKind).toBe("mortgage");
  expect(decoded.activeId).toBe("base");
});

test("base lifecycle events survive a round-trip with regenerated keys", () => {
  const base = baseWith({
    propertyId: "p",
    propertyLifecycleEvents: [{ _id: "lc-orig", kind: "property_sale", month: 120, closingCostPct: 6 }],
  });
  const decoded = scenarioSetFromSearch(scenarioSetToSearch(base, []), bootstrap);
  const events = decoded.base.input.propertyLifecycleEvents;
  expect(events).toHaveLength(1);
  expect(events[0]).toMatchObject({ kind: "property_sale", month: 120, closingCostPct: 6 });
  // The UI-only React key is reminted on decode so base and variants never collide.
  expect(events[0]._id).not.toBe("lc-orig");
  expect(typeof events[0]._id).toBe("string");
});

test("a variant's lifecycle-event override survives a round-trip with regenerated keys", () => {
  const variant = makeVariant("Sell at 10y", {
    propertyId: "p",
    propertyLifecycleEvents: [{ _id: "lc-orig", kind: "property_sale", month: 120, closingCostPct: 6 }],
  });
  const decoded = scenarioSetFromSearch(scenarioSetToSearch(baseWith({}), [variant]), bootstrap);
  const events = decoded.variants[0].overrides.propertyLifecycleEvents;
  expect(events).toHaveLength(1);
  expect(events[0]).toMatchObject({ kind: "property_sale", month: 120, closingCostPct: 6 });
  expect(events[0]._id).not.toBe("lc-orig");
});

test("decoding caps variants at MAX_VARIANTS (untrusted hand-edited link)", () => {
  const variants = Array.from({ length: MAX_VARIANTS + 3 }, (_, index) =>
    makeVariant(`V${index}`, { monthlySpendUsd: 1000 + index })
  );
  const decoded = scenarioSetFromSearch(scenarioSetToSearch(baseWith({}), variants), bootstrap);
  expect(decoded.variants).toHaveLength(MAX_VARIANTS);
});

test("a malformed ?scenarios= blob falls back to a base with no variants", () => {
  const decoded = scenarioSetFromSearch("scenarios=not-json", bootstrap);
  expect(decoded.variants).toHaveLength(0);
  expect(decoded.base.input.monthlySpendUsd).toBe(productInputDefaults(bootstrap).monthlySpendUsd);
});

test("an unrecognized ?scenarios= version falls back to a base with no variants", () => {
  const params = new URLSearchParams();
  params.set("scenarios", JSON.stringify({ v: 999, base: { label: "x", input: {} }, variants: [] }));
  const decoded = scenarioSetFromSearch(params.toString(), bootstrap);
  expect(decoded.variants).toHaveLength(0);
});

// -- Target allocation: seeding, and what reaches the wire ---------------------

const SELLABLE = [
  { symbol: "spy", label: "S&P 500", valueUsd: 900_000 },
  { symbol: "btc", label: "Bitcoin", valueUsd: 100_000 },
];

test("an unedited target allocation seeds from what is held, not equal weights", () => {
  // The whole reason the seed exists: equal weights would make the first refill a rebalance the
  // owner never asked for, dumping a 90/10 split to 50/50 the first time cash crosses the floor.
  expect(resolveSleeveWeights(null, SELLABLE)).toEqual([
    { symbol: "spy", weight: 90 },
    { symbol: "btc", weight: 10 },
  ]);
});

test("a holding too small to round to one percent stays inside the target", () => {
  // Weight 0 means "never sell this", which is not what "you own a little of it" says.
  const weights = resolveSleeveWeights(null, [...SELLABLE, { symbol: "doge", label: "Doge", valueUsd: 100 }]);
  expect(weights.find((sleeve) => sleeve.symbol === "doge").weight).toBe(1);
});

test("an explicit empty target stays empty, and is not re-seeded", () => {
  // `[]` is the user saying "never auto-sell" — an unaffordable month is then ruin. If it were
  // re-seeded like `null`, that choice would be silently un-made every time a request was built.
  expect(resolveSleeveWeights([], SELLABLE)).toEqual([]);
});

test("an explicit zero weight survives to the wire", () => {
  // Zero puts a holding OUTSIDE the target: never sold, and not counted when measuring what is
  // overweight. Dropping it here would put it back in the denominator.
  expect(resolveSleeveWeights([{ symbol: "btc", weight: 0 }], SELLABLE)).toEqual([{ symbol: "btc", weight: 0 }]);
});

test("the scenario always carries an explicit sleeve list, never the unedited null", () => {
  // `FundingPolicy.sleeve_weights` has no "derive it for me" sentinel, and the wire model forbids
  // unknown/extra shapes — so a null leaking out of the editor state is a rejected request.
  const scenario = productScenario(
    { ...productInputDefaults(bootstrap), sleeveWeights: null },
    bootstrap,
    "m",
    12,
    SELLABLE
  );
  expect(scenario.fundingPolicy.sleeveWeights).toEqual([
    { symbol: "spy", weight: 90 },
    { symbol: "btc", weight: 10 },
  ]);
});

test("the cash ceiling is never below the floor", () => {
  // The wire validator rejects an inverted band outright (it has no interior), so the editor must
  // not be able to submit one while the user is mid-edit on the floor.
  const scenario = productScenario(
    { ...productInputDefaults(bootstrap), cashFloorUsd: 50_000, cashCeilingUsd: 10_000 },
    bootstrap,
    "m",
    12,
    SELLABLE
  );
  expect(scenario.fundingPolicy.cashCeilingUsd).toBe(50_000);
});
