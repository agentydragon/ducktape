import assert from "node:assert/strict";
import test from "node:test";

import {
  createDefaultScenarioSetInput,
  decodeScenarioSetUrlState,
  encodeScenarioSetUrlState,
  normalizeScenarioSetInput,
  patchScenarioInputSection,
  scenarioSetInputToRequest,
} from "./scenario_set_state.js";
import { decamelizeObjectKeys } from "./casing.js";
import { zScenarioSetInput } from "./api/schema.zod.mjs";

const bootstrap = {
  defaultPropertyId: "location_a_property",
  defaultActorPolicy: "owner_only",
  defaultOwnerResidenceMode: "selected_property",
  defaultRentalUsePolicy: "not_rented",
  defaultLiquidReservePolicy: "none",
  defaultInitialCheckingUsd: 25_000,
  defaultCheckingFloorUsd: 10_000,
  defaultCheckingSaleAmountUsd: 20_000,
  defaultPartnerMonthlyPaymentUsd: 2_435,
  defaultRolloutSamples: 16,
  financeSnapshot: {
    asOfDate: "2026-05-14",
    cashUsd: 26_000,
    wealthfrontSp500Usd: 61_000,
    ibkrVtUsd: 39_000,
    sp500ProxyPortfolioUsd: 100_000,
    concentratedHoldings: [
      {
        holdingId: "private_holding_a",
        label: "Private Holding A",
        units: 500,
        fmvUsdPerUnit: 20,
        valueUsd: 10_000,
        valuationSource: "fixture mark",
      },
    ],
  },
  defaultScenarios: [
    { propertyId: "location_a_property", actorPolicy: "owner_only" },
    { propertyId: "location_b_property", actorPolicy: "owner_plus_partner", label: "Location B shared" },
  ],
  defaultKnobs: {
    holdYears: 5,
    startingPortfolioUsd: 100_000,
    downPaymentPct: 25,
    financingMode: "fixed_30",
    customMortgageRate: 6.5,
    customMortgageTermYears: 30,
    creditScore: 776,
    vacancyPct: 5,
    roomVacancyPct: 5,
    mgmtPct: 8,
    leasingFeePct: 0,
    roomsRentedWhileLiving: 0,
    roomRentMonthlyUsd: 1_800,
    maintenancePct: 1,
    insuranceAnnualUsd: 2_400,
    closingCostBuyPct: 2.5,
    closingCostSellPct: 6.5,
    depreciableBasisPct: 80,
  },
  actorPolicyOptions: [
    { id: "owner_only", label: "Alpha only" },
    { id: "owner_plus_partner", label: "Alpha + Beta" },
  ],
  ownerResidenceModeOptions: [
    { id: "selected_property", label: "Selected" },
    { id: "rental_elsewhere", label: "Elsewhere" },
  ],
  agents: [
    { actorId: "alpha", label: "Alpha", role: "primary_owner" },
    { actorId: "beta", label: "Beta", role: "equity_building_occupant" },
  ],
  rentalUsePolicyOptions: [
    { id: "not_rented", label: "Not rented" },
    { id: "rent_rooms_while_owner_lives_there", label: "Rent rooms" },
    { id: "rent_whole_property", label: "Rent whole property" },
  ],
  liquidReservePolicyOptions: [
    { id: "none", label: "None" },
    { id: "checking_floor_sp500", label: "Sell SP500" },
  ],
  locations: [
    {
      id: "location_a",
      label: "Location A",
      localRegulation: { propertyTaxAnnualPct: 1, localTransferTaxPct: 0, specialAssessmentAnnualUsd: 0 },
    },
    {
      id: "location_b",
      label: "Location B",
      localRegulation: { propertyTaxAnnualPct: 1, localTransferTaxPct: 0, specialAssessmentAnnualUsd: 0 },
    },
  ],
  properties: [
    {
      id: "location_a_property",
      locationId: "location_a",
      address: "Location A Property",
      priceUsd: 998_000,
      hoaMonthlyUsd: 321,
      rentEstimateUsd: 4_200,
      beds: 3,
    },
    {
      id: "location_b_property",
      locationId: "location_b",
      address: "Location B Property",
      priceUsd: 520_000,
      rentEstimateUsd: 3_100,
      beds: 4,
    },
  ],
};

function patchScenarioSections(scenario, sectionPatches) {
  return Object.entries(sectionPatches).reduce(
    (nextScenario, [section, patch]) => patchScenarioInputSection(nextScenario, section, patch),
    scenario
  );
}

// Recursively collect keys present in `input` that the Zod parse stripped —
// i.e. fields the wire schema doesn't know about. Required-fields coverage
// comes from `parse()` itself (it throws on a missing required key).
function strippedKeys(input, parsed, path = "") {
  if (Array.isArray(input)) return input.flatMap((it, i) => strippedKeys(it, parsed?.[i], `${path}[${i}]`));
  if (input && typeof input === "object") {
    return Object.entries(input).flatMap(([k, v]) =>
      parsed && k in parsed ? strippedKeys(v, parsed[k], `${path}.${k}`) : [`${path}.${k}`]
    );
  }
  return [];
}

test("default input creates comparable generic location scenarios", () => {
  const input = createDefaultScenarioSetInput(bootstrap);
  const firstScenario = input.scenarios[0];
  const secondScenario = input.scenarios[1];

  assert.equal(input.scenarios.length, 2);
  assert.equal(firstScenario.propertyAndLocation.propertyId, "location_a_property");
  assert.equal(firstScenario.initialBalanceSheet.initialCheckingUsd, 25_000);
  assert.equal(firstScenario.initialBalanceSheet.startingPortfolioUsd, 100_000);
  assert.equal(firstScenario.initialBalanceSheet.privateEquityUnits, 500);
  assert.equal(firstScenario.policies.privateEquitySalePolicy, "none");
  assert.equal(secondScenario.propertyAndLocation.propertyId, "location_b_property");
  assert.equal(secondScenario.actorsAndOwnership.actorPolicy, "owner_plus_partner");
  assert.equal(input.marketRequest.seed, 0);
  assert.equal(input.reportSpec.includeMonthlyColumns, true);
});

test("normalized scenario input stays in nested domain sections", () => {
  const input = normalizeScenarioSetInput(createDefaultScenarioSetInput(bootstrap), bootstrap);
  const scenario = input.scenarios[0];

  assert.equal(scenario.identity.scenarioId, "scenario_1");
  assert.equal(scenario.propertyAndLocation.propertyId, "location_a_property");
  assert.equal(scenario.financing.financingMode, "fixed_30");
  assert.equal(scenario.taxAccounting.closingCostBuyPct, 2.5);
  assert.equal(scenario.initialBalanceSheet.privateEquityUnits, 500);
  assert.equal(scenario.policies.privateEquitySalePolicy, "none");
  assert.equal(scenario.scenarioId, undefined);
  assert.equal(scenario.propertyId, undefined);
  assert.equal(scenario.privateEquityUnits, undefined);
});

test("section patch updates one nested domain section", () => {
  const input = normalizeScenarioSetInput(createDefaultScenarioSetInput(bootstrap), bootstrap);
  const original = input.scenarios[0];
  const changed = patchScenarioInputSection(original, "financing", {
    financingMode: "custom",
    downPaymentPct: 35,
  });

  assert.equal(changed.financing.financingMode, "custom");
  assert.equal(changed.financing.downPaymentPct, 35);
  assert.equal(changed.identity.label, original.identity.label);
  assert.equal(changed.taxAccounting.closingCostBuyPct, original.taxAccounting.closingCostBuyPct);
  assert.throws(() => patchScenarioInputSection(original, "unknown", {}), /Unknown Augur scenario input section/);
});

test("scenario set request is canonical backend input after decamelizing", () => {
  const input = normalizeScenarioSetInput(createDefaultScenarioSetInput(bootstrap), bootstrap);
  input.scenarios[0] = patchScenarioSections(input.scenarios[0], {
    financing: {
      financingMode: "custom",
      downPaymentPct: 42,
      customMortgageRate: 7.25,
      customMortgageTermYears: 18,
      creditScore: 701,
    },
    occupancyAndRental: {
      rentalUsePolicy: "rent_rooms_while_owner_lives_there",
      vacancyPct: 12,
      managementFeePct: 9.5,
      leasingFeePct: 50,
      roomsRentedWhileLiving: 2,
      roomRentMonthlyUsd: 1_650,
      roomVacancyPct: 11,
    },
    propertyAssumptions: {
      maintenancePct: 1.4,
      insuranceAnnualUsd: 3_200,
      depreciableBasisPct: 75,
    },
    taxAccounting: {
      closingCostBuyPct: 3.1,
      closingCostSellPct: 5.9,
    },
    initialBalanceSheet: {
      privateEquityUnits: 456,
    },
    policies: {
      liquidReservePolicy: "checking_floor_sp500",
      privateEquitySalePolicy: "liquid_net_worth_floor",
      privateEquityLiquidNetWorthFloorUsd: 300_000,
      privateEquityTenderSaleAmountUsd: 75_000,
    },
  });
  const request = scenarioSetInputToRequest(input, bootstrap);
  const backendRequest = decamelizeObjectKeys(request);
  const firstScenario = backendRequest.scenarios[0];
  const purchaseEvent = firstScenario.events.find((event) => event.event_type === "property_purchase");
  const mortgageEvent = firstScenario.events.find((event) => event.event_type === "mortgage_origination");

  assert.equal(backendRequest.scenario_set_id, "augur_futures_explorer");
  assert.equal(backendRequest.report_spec.include_monthly_columns, true);
  assert.deepEqual(Object.keys(backendRequest.report_spec).sort(), ["include_monthly_columns", "percentiles"]);
  assert.deepEqual(firstScenario.property_selection, { property_id: "location_a_property" });
  assert.equal(firstScenario.tax_regimes, undefined);
  assert.equal(firstScenario.financing.financing_mode, "custom");
  assert.equal(firstScenario.financing.down_payment_pct, 42);
  assert.equal(firstScenario.financing.mortgage_rate_pct, 7.25);
  assert.equal(firstScenario.financing.mortgage_term_years, 18);
  assert.equal(firstScenario.financing.credit_score, 701);
  assert.equal(firstScenario.policies[0].policy_type, "checking_floor_sell_public_stock");
  assert.equal(firstScenario.policies[0].floor_usd, 10_000);
  assert.equal(firstScenario.policies[0].sale_amount_usd, 20_000);
  assert.deepEqual(
    firstScenario.policies.find((policy) => policy.policy_type === "private_equity_sale"),
    {
      policy_id: "private_equity_liquid_floor_sale",
      policy_type: "private_equity_sale",
      actor_id: "alpha",
      enabled: true,
      proceeds_destination: "generic_sp500_stock",
      sale_rule: {
        sale_rule_type: "liquid_net_worth_floor",
        min_liquid_net_worth_usd: 300_000,
        sale_amount_usd: 75_000,
      },
    }
  );
  assert.equal(firstScenario.rental_plan.rental_mode, "rent_rooms_while_owner_lives_there");
  assert.equal(firstScenario.rental_plan.rooms_rented, 2);
  assert.equal(firstScenario.rental_plan.room_rent_monthly_usd, 1_650);
  assert.equal(firstScenario.rental_plan.vacancy_pct, 12);
  assert.equal(firstScenario.rental_plan.room_vacancy_pct, 11);
  assert.equal(firstScenario.rental_plan.management_fee_pct, 9.5);
  assert.equal(firstScenario.rental_plan.leasing_fee_pct, 50);
  assert.equal(purchaseEvent.hoa_monthly_usd, 321);
  assert.equal(firstScenario.tax_profile, undefined);
  assert.deepEqual(firstScenario.transaction_costs, {
    closing_cost_buy_pct: 3.1,
    closing_cost_sell_pct: 5.9,
  });
  assert.deepEqual(firstScenario.property_assumptions, {
    insurance_annual_usd: 3_200,
    maintenance_pct: 1.4,
    depreciable_basis_pct: 75,
  });
  assert.ok(Math.abs(mortgageEvent.amount_usd - 578_840) < 1e-6);
  assert.equal(
    firstScenario.events.find((event) => event.event_type.startsWith("private_equity_")),
    undefined
  );
  assert.deepEqual(
    firstScenario.initial_balance_sheet.assets.find((asset) => asset.asset_type === "private_equity"),
    {
      asset_id: "private_equity_private",
      asset_type: "private_equity",
      owner_actor_id: "alpha",
      value_usd: 9_120,
      units: 456,
      cost_basis_usd: 0,
    }
  );
  assert.equal(backendRequest.scenarios[1].policies[0].policy_type, "partner_equity_accrual");
  assert.equal(backendRequest.scenarios[1].policies[0].actor_id, "beta");
  assert.equal(backendRequest.scenarios[1].policies[0].base_monthly_payment_usd, 2_435);
});

test("backend request mapper output is covered by generated schema", () => {
  const input = normalizeScenarioSetInput(createDefaultScenarioSetInput(bootstrap), bootstrap);
  input.scenarios[0] = patchScenarioSections(input.scenarios[0], {
    financing: {
      financingMode: "custom",
      customMortgageRate: 7.125,
      customMortgageTermYears: 20,
    },
    occupancyAndRental: {
      rentalUsePolicy: "rent_rooms_while_owner_lives_there",
      roomsRentedWhileLiving: 1,
    },
    policies: {
      liquidReservePolicy: "checking_floor_sp500",
    },
  });
  const backendRequest = decamelizeObjectKeys(scenarioSetInputToRequest(input, bootstrap));
  assert.deepEqual(strippedKeys(backendRequest, zScenarioSetInput.parse(backendRequest)), []);
});

test("request normalization only sends current report fields", () => {
  const input = createDefaultScenarioSetInput(bootstrap);
  input.reportSpec.includeMonthlyColumns = false;

  const request = scenarioSetInputToRequest(input, bootstrap);
  const backendRequest = decamelizeObjectKeys(request);

  assert.equal("shared_market_paths" in backendRequest.market_request, false);
  assert.deepEqual(Object.keys(backendRequest.report_spec).sort(), ["include_monthly_columns", "percentiles"]);
  assert.equal(backendRequest.report_spec.include_monthly_columns, false);
});

test("URL state round-trips only input state", () => {
  const input = createDefaultScenarioSetInput(bootstrap);
  const polluted = {
    ...input,
    scenarioResults: [{ scenarioId: "scenario_1", summary: { netWorthUsd: 123 } }],
    scenarios: input.scenarios.map((scenario) => ({ ...scenario, backendResult: { shouldNotPersist: true } })),
  };
  const encoded = encodeScenarioSetUrlState(polluted);
  const decoded = decodeScenarioSetUrlState(encoded);

  assert.deepEqual(decoded.scenarioResults, undefined);
  assert.deepEqual(decoded.scenarios[0].backendResult, undefined);
  assert.equal(decoded.scenarios[0].propertyAndLocation.propertyId, "location_a_property");
  assert.equal(decoded.scenarios[0].financing.customMortgageRate, undefined);
  assert.equal(decoded.scenarios[0].financing.customMortgageTermYears, undefined);
});

test("URL state round-trips rich scenario controls in camelCase", () => {
  const input = normalizeScenarioSetInput(createDefaultScenarioSetInput(bootstrap), bootstrap);
  input.scenarios[0] = patchScenarioSections(input.scenarios[0], {
    financing: {
      financingMode: "custom",
      downPaymentPct: 35,
      customMortgageRate: 7.125,
      customMortgageTermYears: 20,
    },
    taxAccounting: {
      closingCostBuyPct: 3.7,
    },
    occupancyAndRental: {
      vacancyPct: 7,
    },
    initialBalanceSheet: {
      privateEquityUnits: 1_234,
    },
    policies: {
      privateEquitySalePolicy: "liquid_net_worth_floor",
      privateEquityLiquidNetWorthFloorUsd: 250_000,
      privateEquityTenderSaleAmountUsd: 60_000,
    },
  });

  const decoded = decodeScenarioSetUrlState(encodeScenarioSetUrlState(input));

  assert.equal(decoded.scenarios[0].financing.financingMode, "custom");
  assert.equal(decoded.scenarios[0].financing.downPaymentPct, 35);
  assert.equal(decoded.scenarios[0].financing.customMortgageRate, 7.125);
  assert.equal(decoded.scenarios[0].financing.customMortgageTermYears, 20);
  assert.equal(decoded.scenarios[0].taxAccounting.closingCostBuyPct, 3.7);
  assert.equal(decoded.scenarios[0].occupancyAndRental.vacancyPct, 7);
  assert.equal(decoded.scenarios[0].initialBalanceSheet.privateEquityUnits, 1_234);
  assert.equal(decoded.scenarios[0].policies.privateEquitySalePolicy, "liquid_net_worth_floor");
  assert.equal(decoded.scenarios[0].policies.privateEquityLiquidNetWorthFloorUsd, 250_000);
  assert.equal(decoded.scenarios[0].policies.privateEquityTenderSaleAmountUsd, 60_000);
});

test("URL state normalizes missing trajectory seed to deterministic default", () => {
  const input = createDefaultScenarioSetInput(bootstrap);
  const decoded = decodeScenarioSetUrlState(encodeScenarioSetUrlState(input));
  decoded.marketRequest.seed = null;

  const normalized = normalizeScenarioSetInput(decoded, bootstrap);

  assert.equal(normalized.marketRequest.seed, 0);
});
