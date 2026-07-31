// Unit tests for the scenario-set URL codec (Base + per-variant overrides). Runs under vitest;
// see //augur/frontend:input_helpers_test.

import { test, expect } from "vitest";

import {
  scenarioSetToSearch,
  scenarioSetFromSearch,
  makeVariant,
  resolveVariant,
  productInputDefaults,
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
