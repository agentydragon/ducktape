// Unit tests for the scenario-set URL codec (multi-scenario comparison). Runs under vitest;
// see //augur/frontend:input_helpers_test.

import { test, expect } from "vitest";

import {
  scenarioSetToSearch,
  scenarioSetFromSearch,
  makeScenarioEntry,
  productInputDefaults,
  productInputToSearch,
} from "./input_helpers.ts";

// `productInputDefaults` only reads `productInputDefaults` (deployment overrides) and
// `locations[0]?.id`; an empty bootstrap exercises the pure hard-coded defaults.
const bootstrap = { productInputDefaults: {}, locations: [] };

function entry(overrides, label) {
  return makeScenarioEntry({ ...productInputDefaults(bootstrap), ...overrides }, label);
}

test("a single scenario encodes as ?s= (not ?scenarios=) and round-trips", () => {
  const only = entry({ monthlySpendUsd: 4200 }, "Scenario 1");
  const search = scenarioSetToSearch([only], only.id, bootstrap);
  expect(search).toMatch(/(^|&)s=/);
  expect(search).not.toContain("scenarios=");

  const decoded = scenarioSetFromSearch(search, bootstrap);
  expect(decoded.scenarios).toHaveLength(1);
  expect(decoded.scenarios[0].input.monthlySpendUsd).toBe(4200);
  expect(decoded.activeId).toBe(decoded.scenarios[0].id);
});

test("a pre-multi ?s= link decodes as a 1-element set (backward compatible)", () => {
  const legacy = productInputToSearch({ ...productInputDefaults(bootstrap), monthlySpendUsd: 9100 }, bootstrap);
  const decoded = scenarioSetFromSearch(legacy, bootstrap);
  expect(decoded.scenarios).toHaveLength(1);
  expect(decoded.scenarios[0].input.monthlySpendUsd).toBe(9100);
});

test("two scenarios encode as ?scenarios= and round-trip inputs, labels, and active selection", () => {
  const rent = entry({ monthlyRentUsd: 3000, monthlySpendUsd: 5000 }, "Rent");
  const buy = entry({ financingKind: "mortgage", monthlySpendUsd: 5000 }, "Mortgage");
  const search = scenarioSetToSearch([rent, buy], buy.id, bootstrap);
  expect(search).toContain("scenarios=");
  expect(search).not.toMatch(/(^|&)s=/);

  const decoded = scenarioSetFromSearch(search, bootstrap);
  expect(decoded.scenarios).toHaveLength(2);
  expect(decoded.scenarios.map((s) => s.label)).toEqual(["Rent", "Mortgage"]);
  expect(decoded.scenarios[0].input.monthlyRentUsd).toBe(3000);
  expect(decoded.scenarios[1].input.financingKind).toBe("mortgage");
  // Active was the second scenario; the decoded active id points at the second entry.
  expect(decoded.activeId).toBe(decoded.scenarios[1].id);
});

test("lifecycle events survive a multi-scenario round-trip with regenerated keys", () => {
  const withLifecycle = entry(
    {
      propertyId: "p",
      propertyLifecycleEvents: [{ _id: "lc-orig", kind: "property_sale", month: 120, closingCostPct: 6 }],
    },
    "Sell at 10y"
  );
  const other = entry({}, "Hold");
  const decoded = scenarioSetFromSearch(
    scenarioSetToSearch([withLifecycle, other], withLifecycle.id, bootstrap),
    bootstrap
  );
  const events = decoded.scenarios[0].input.propertyLifecycleEvents;
  expect(events).toHaveLength(1);
  expect(events[0]).toMatchObject({ kind: "property_sale", month: 120, closingCostPct: 6 });
  // The UI-only React key is reminted on decode so sibling scenarios never collide.
  expect(events[0]._id).not.toBe("lc-orig");
  expect(typeof events[0]._id).toBe("string");
});

test("a malformed ?scenarios= blob falls back to a single default scenario", () => {
  const decoded = scenarioSetFromSearch("scenarios=not-json", bootstrap);
  expect(decoded.scenarios).toHaveLength(1);
  expect(decoded.scenarios[0].input.monthlySpendUsd).toBe(productInputDefaults(bootstrap).monthlySpendUsd);
});

test("an unrecognized ?scenarios= version falls back to a single default scenario", () => {
  const params = new URLSearchParams();
  params.set("scenarios", JSON.stringify({ v: 999, active: 0, scenarios: [{ label: "x", input: {} }] }));
  const decoded = scenarioSetFromSearch(params.toString(), bootstrap);
  expect(decoded.scenarios).toHaveLength(1);
});
