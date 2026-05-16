import { camelizeObjectKeys, decamelizeObjectKeys, snakeToCamelKey } from "./casing.js";
import { zBrowserScenarioInputInput, zFinancingMode, zPrivateEquitySalePolicyId } from "./api/schema.zod.mjs";

export const SCENARIO_COLORS = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2"];

const URL_STATE_VERSION = 4;

const DEFAULT_MARKET_REQUEST = {
  marketModelId: "current_market_model",
  rolloutCount: 128,
  horizonMonths: 360,
  seed: 0,
};

const DEFAULT_REPORT_SPEC = {
  percentiles: [5, 25, 50, 75, 95],
  includeMonthlyColumns: true,
};

const FINANCING_MODE_IDS = new Set(zFinancingMode.options);
const PRIVATE_EQUITY_SALE_POLICY_IDS = new Set(zPrivateEquitySalePolicyId.options);
const SCENARIO_INPUT_SECTIONS = new Set(Object.keys(zBrowserScenarioInputInput.shape).map(snakeToCamelKey));

function finiteNumber(value, defaultValue) {
  const number = Number(value);
  return Number.isFinite(number) ? number : defaultValue;
}

function nullableNumber(value, defaultValue = null) {
  if (value === null || value === undefined || value === "") return defaultValue;
  return finiteNumber(value, defaultValue);
}

function positiveNumber(value, defaultValue) {
  const number = finiteNumber(value, defaultValue);
  return number > 0 ? number : defaultValue;
}

function nullableInteger(value, defaultValue = null) {
  if (value === null || value === undefined || value === "") return defaultValue;
  const number = Number(value);
  return Number.isInteger(number) ? number : defaultValue;
}

function optionIds(options) {
  return new Set((options ?? []).map((option) => option.id));
}

function defaultOption(options, defaultId) {
  return options?.[0]?.id ?? defaultId;
}

function propertyById(bootstrap, propertyId) {
  return bootstrap?.properties?.find((property) => property.id === propertyId) ?? bootstrap?.properties?.[0] ?? null;
}

function defaultPropertyId(bootstrap) {
  return bootstrap?.defaultPropertyId ?? bootstrap?.properties?.[0]?.id ?? null;
}

function defaultConcentratedHolding(bootstrap) {
  return bootstrap?.financeSnapshot?.concentratedHoldings?.[0] ?? null;
}

function holdingValueUsd(holding) {
  return finiteNumber(holding?.valueUsd, finiteNumber(holding?.units, 0) * finiteNumber(holding?.fmvUsdPerUnit, 0));
}

function scenarioSection(scenario, section) {
  const value = scenario?.[section];
  return value && typeof value === "object" ? value : {};
}

export function privateEquityCurrentUnitPriceUsd(bootstrap) {
  const holding = defaultConcentratedHolding(bootstrap);
  const fmv = finiteNumber(holding?.fmvUsdPerUnit, NaN);
  if (Number.isFinite(fmv)) return fmv;
  const units = finiteNumber(holding?.units, 0);
  return units > 0 ? holdingValueUsd(holding) / units : 0;
}

export function privateEquityValueUsdForUnits(bootstrap, units) {
  return Math.max(0, finiteNumber(units, 0)) * privateEquityCurrentUnitPriceUsd(bootstrap);
}

function scenarioIdFromIndex(index) {
  return `scenario_${index + 1}`;
}

export function patchScenarioInputSection(scenario, section, patch) {
  if (!SCENARIO_INPUT_SECTIONS.has(section)) {
    throw new Error(`Unknown Augur scenario input section: ${section}`);
  }
  return {
    ...scenario,
    [section]: {
      ...(scenario?.[section] ?? {}),
      ...(patch ?? {}),
    },
  };
}

export function uniqueScenarioId(existingScenarioIds, base = "scenario") {
  const existing = new Set(existingScenarioIds);
  let index = 1;
  let candidate = `${base}_${index}`;
  while (existing.has(candidate)) {
    index += 1;
    candidate = `${base}_${index}`;
  }
  return candidate;
}

export function createScenarioInput(bootstrap, overrides = {}) {
  const index = finiteNumber(overrides.index, 0);
  const defaultScenarioPropertyId = defaultPropertyId(bootstrap);
  const propertyId = overrides.propertyId ?? defaultScenarioPropertyId;
  const property = propertyById(bootstrap, propertyId);
  const defaultKnobs = bootstrap?.defaultKnobs ?? {};
  const holding = defaultConcentratedHolding(bootstrap);
  const privateEquityUnits = finiteNumber(overrides.privateEquityUnits, holding?.units ?? 0);
  return {
    identity: {
      scenarioId: overrides.scenarioId ?? scenarioIdFromIndex(index),
      label: overrides.label ?? (property?.address ? `${property.address}` : `Scenario ${index + 1}`),
      enabled: overrides.enabled ?? true,
      color: overrides.color ?? SCENARIO_COLORS[index % SCENARIO_COLORS.length],
    },
    propertyAndLocation: {
      propertyId,
    },
    actorsAndOwnership: {
      actorPolicy:
        overrides.actorPolicy ??
        bootstrap?.defaultActorPolicy ??
        defaultOption(bootstrap?.actorPolicyOptions, "owner_only"),
      partnerPaymentMonthlyUsd: finiteNumber(
        overrides.partnerPaymentMonthlyUsd,
        bootstrap?.defaultPartnerMonthlyPaymentUsd ?? 0
      ),
    },
    timeline: {
      holdYears: positiveNumber(overrides.holdYears, defaultKnobs.holdYears ?? 5),
    },
    financing: {
      financingMode: FINANCING_MODE_IDS.has(overrides.financingMode)
        ? overrides.financingMode
        : (defaultKnobs.financingMode ?? "fixed_30"),
      downPaymentPct: finiteNumber(overrides.downPaymentPct, defaultKnobs.downPaymentPct ?? 25),
      customMortgageRate: nullableNumber(overrides.customMortgageRate, defaultKnobs.customMortgageRate ?? null),
      customMortgageTermYears: positiveNumber(
        overrides.customMortgageTermYears,
        defaultKnobs.customMortgageTermYears ?? 30
      ),
      creditScore: nullableNumber(overrides.creditScore, defaultKnobs.creditScore ?? null),
    },
    occupancyAndRental: {
      ownerResidenceMode:
        overrides.ownerResidenceMode ??
        bootstrap?.defaultOwnerResidenceMode ??
        defaultOption(bootstrap?.ownerResidenceModeOptions, "selected_property"),
      rentalUsePolicy:
        overrides.rentalUsePolicy ??
        bootstrap?.defaultRentalUsePolicy ??
        defaultOption(bootstrap?.rentalUsePolicyOptions, "not_rented"),
      vacancyPct: finiteNumber(overrides.vacancyPct, defaultKnobs.vacancyPct ?? 0),
      managementFeePct: finiteNumber(overrides.managementFeePct, defaultKnobs.mgmtPct ?? 0),
      leasingFeePct: finiteNumber(overrides.leasingFeePct, defaultKnobs.leasingFeePct ?? 0),
      roomsRentedWhileLiving: finiteNumber(overrides.roomsRentedWhileLiving, defaultKnobs.roomsRentedWhileLiving ?? 0),
      roomRentMonthlyUsd: finiteNumber(overrides.roomRentMonthlyUsd, defaultKnobs.roomRentMonthlyUsd ?? 0),
      roomVacancyPct: finiteNumber(overrides.roomVacancyPct, defaultKnobs.roomVacancyPct ?? 0),
    },
    propertyAssumptions: {
      maintenancePct: finiteNumber(overrides.maintenancePct, defaultKnobs.maintenancePct ?? 1),
      insuranceAnnualUsd: finiteNumber(overrides.insuranceAnnualUsd, defaultKnobs.insuranceAnnualUsd ?? 0),
      depreciableBasisPct: finiteNumber(overrides.depreciableBasisPct, defaultKnobs.depreciableBasisPct ?? 0),
    },
    taxAccounting: {
      closingCostBuyPct: finiteNumber(overrides.closingCostBuyPct, defaultKnobs.closingCostBuyPct ?? 0),
      closingCostSellPct: finiteNumber(overrides.closingCostSellPct, defaultKnobs.closingCostSellPct ?? 0),
    },
    initialBalanceSheet: {
      initialCheckingUsd: finiteNumber(overrides.initialCheckingUsd, bootstrap?.defaultInitialCheckingUsd ?? 25_000),
      startingPortfolioUsd: finiteNumber(
        overrides.startingPortfolioUsd,
        defaultKnobs.startingPortfolioUsd ?? bootstrap?.financeSnapshot?.sp500ProxyPortfolioUsd ?? 0
      ),
      privateEquityUnits,
    },
    policies: {
      liquidReservePolicy:
        overrides.liquidReservePolicy ??
        bootstrap?.defaultLiquidReservePolicy ??
        defaultOption(bootstrap?.liquidReservePolicyOptions, "none"),
      checkingFloorUsd: finiteNumber(overrides.checkingFloorUsd, bootstrap?.defaultCheckingFloorUsd ?? 10_000),
      checkingSaleAmountUsd: positiveNumber(
        overrides.checkingSaleAmountUsd,
        bootstrap?.defaultCheckingSaleAmountUsd ?? 20_000
      ),
      privateEquitySalePolicy: PRIVATE_EQUITY_SALE_POLICY_IDS.has(overrides.privateEquitySalePolicy)
        ? overrides.privateEquitySalePolicy
        : "none",
      privateEquityLiquidNetWorthFloorUsd: finiteNumber(overrides.privateEquityLiquidNetWorthFloorUsd, 0),
      privateEquityTenderSaleAmountUsd: positiveNumber(overrides.privateEquityTenderSaleAmountUsd, 50_000),
    },
  };
}

export function createDefaultScenarioSetInput(bootstrap) {
  const defaultScenarioSpecs =
    Array.isArray(bootstrap?.defaultScenarios) && bootstrap.defaultScenarios.length > 0
      ? bootstrap.defaultScenarios
      : [{ propertyId: defaultPropertyId(bootstrap), actorPolicy: bootstrap?.defaultActorPolicy }];
  const scenarios = defaultScenarioSpecs.map((spec, index) =>
    createScenarioInput(bootstrap, {
      index,
      propertyId: spec.propertyId,
      actorPolicy: spec.actorPolicy,
      label: spec.label,
    })
  );
  return {
    title: "Augur futures comparison",
    marketRequest: {
      ...DEFAULT_MARKET_REQUEST,
      rolloutCount: bootstrap?.defaultRolloutSamples ?? DEFAULT_MARKET_REQUEST.rolloutCount,
    },
    reportSpec: DEFAULT_REPORT_SPEC,
    scenarios,
  };
}

function normalizeScenarioInput(scenario, bootstrap, index, existingIds) {
  const propertyIds = new Set((bootstrap?.properties ?? []).map((property) => property.id));
  const actorPolicyIds = optionIds(bootstrap?.actorPolicyOptions);
  const ownerResidenceModeIds = optionIds(bootstrap?.ownerResidenceModeOptions);
  const rentalUsePolicyIds = optionIds(bootstrap?.rentalUsePolicyOptions);
  const liquidReservePolicyIds = optionIds(bootstrap?.liquidReservePolicyOptions);
  const defaultScenario = createScenarioInput(bootstrap, { index });
  const identity = scenarioSection(scenario, "identity");
  const propertyAndLocation = scenarioSection(scenario, "propertyAndLocation");
  const actorsAndOwnership = scenarioSection(scenario, "actorsAndOwnership");
  const timeline = scenarioSection(scenario, "timeline");
  const financing = scenarioSection(scenario, "financing");
  const occupancyAndRental = scenarioSection(scenario, "occupancyAndRental");
  const propertyAssumptions = scenarioSection(scenario, "propertyAssumptions");
  const taxAccounting = scenarioSection(scenario, "taxAccounting");
  const initialBalanceSheet = scenarioSection(scenario, "initialBalanceSheet");
  const policies = scenarioSection(scenario, "policies");
  const defaultIdentity = defaultScenario.identity;
  const defaultPropertyAndLocation = defaultScenario.propertyAndLocation;
  const defaultActorsAndOwnership = defaultScenario.actorsAndOwnership;
  const defaultTimeline = defaultScenario.timeline;
  const defaultFinancing = defaultScenario.financing;
  const defaultOccupancyAndRental = defaultScenario.occupancyAndRental;
  const defaultPropertyAssumptions = defaultScenario.propertyAssumptions;
  const defaultTaxAccounting = defaultScenario.taxAccounting;
  const defaultInitialBalanceSheet = defaultScenario.initialBalanceSheet;
  const defaultPolicies = defaultScenario.policies;
  const privateEquityUnits = finiteNumber(
    initialBalanceSheet.privateEquityUnits,
    defaultInitialBalanceSheet.privateEquityUnits
  );
  const scenarioId =
    typeof identity.scenarioId === "string" && /^[a-z0-9][a-z0-9_-]*$/.test(identity.scenarioId)
      ? identity.scenarioId
      : uniqueScenarioId(existingIds, "scenario");
  existingIds.add(scenarioId);
  return {
    identity: {
      scenarioId,
      label:
        typeof identity.label === "string" && identity.label.trim() ? identity.label.trim() : defaultIdentity.label,
      enabled: Boolean(identity.enabled ?? defaultIdentity.enabled),
      color: typeof identity.color === "string" && identity.color ? identity.color : defaultIdentity.color,
    },
    propertyAndLocation: {
      propertyId: propertyIds.has(propertyAndLocation.propertyId)
        ? propertyAndLocation.propertyId
        : defaultPropertyAndLocation.propertyId,
    },
    actorsAndOwnership: {
      actorPolicy: actorPolicyIds.has(actorsAndOwnership.actorPolicy)
        ? actorsAndOwnership.actorPolicy
        : defaultActorsAndOwnership.actorPolicy,
      partnerPaymentMonthlyUsd: finiteNumber(
        actorsAndOwnership.partnerPaymentMonthlyUsd,
        defaultActorsAndOwnership.partnerPaymentMonthlyUsd
      ),
    },
    timeline: {
      holdYears: positiveNumber(timeline.holdYears, defaultTimeline.holdYears),
    },
    financing: {
      financingMode: FINANCING_MODE_IDS.has(financing.financingMode)
        ? financing.financingMode
        : defaultFinancing.financingMode,
      downPaymentPct: finiteNumber(financing.downPaymentPct, defaultFinancing.downPaymentPct),
      customMortgageRate: nullableNumber(financing.customMortgageRate, defaultFinancing.customMortgageRate),
      customMortgageTermYears: positiveNumber(
        financing.customMortgageTermYears,
        defaultFinancing.customMortgageTermYears
      ),
      creditScore: nullableNumber(financing.creditScore, defaultFinancing.creditScore),
    },
    occupancyAndRental: {
      ownerResidenceMode: ownerResidenceModeIds.has(occupancyAndRental.ownerResidenceMode)
        ? occupancyAndRental.ownerResidenceMode
        : defaultOccupancyAndRental.ownerResidenceMode,
      rentalUsePolicy: rentalUsePolicyIds.has(occupancyAndRental.rentalUsePolicy)
        ? occupancyAndRental.rentalUsePolicy
        : defaultOccupancyAndRental.rentalUsePolicy,
      vacancyPct: finiteNumber(occupancyAndRental.vacancyPct, defaultOccupancyAndRental.vacancyPct),
      managementFeePct: finiteNumber(occupancyAndRental.managementFeePct, defaultOccupancyAndRental.managementFeePct),
      leasingFeePct: finiteNumber(occupancyAndRental.leasingFeePct, defaultOccupancyAndRental.leasingFeePct),
      roomsRentedWhileLiving: finiteNumber(
        occupancyAndRental.roomsRentedWhileLiving,
        defaultOccupancyAndRental.roomsRentedWhileLiving
      ),
      roomRentMonthlyUsd: finiteNumber(
        occupancyAndRental.roomRentMonthlyUsd,
        defaultOccupancyAndRental.roomRentMonthlyUsd
      ),
      roomVacancyPct: finiteNumber(occupancyAndRental.roomVacancyPct, defaultOccupancyAndRental.roomVacancyPct),
    },
    propertyAssumptions: {
      maintenancePct: finiteNumber(propertyAssumptions.maintenancePct, defaultPropertyAssumptions.maintenancePct),
      insuranceAnnualUsd: finiteNumber(
        propertyAssumptions.insuranceAnnualUsd,
        defaultPropertyAssumptions.insuranceAnnualUsd
      ),
      depreciableBasisPct: finiteNumber(
        propertyAssumptions.depreciableBasisPct,
        defaultPropertyAssumptions.depreciableBasisPct
      ),
    },
    taxAccounting: {
      closingCostBuyPct: finiteNumber(taxAccounting.closingCostBuyPct, defaultTaxAccounting.closingCostBuyPct),
      closingCostSellPct: finiteNumber(taxAccounting.closingCostSellPct, defaultTaxAccounting.closingCostSellPct),
    },
    initialBalanceSheet: {
      initialCheckingUsd: finiteNumber(
        initialBalanceSheet.initialCheckingUsd,
        defaultInitialBalanceSheet.initialCheckingUsd
      ),
      startingPortfolioUsd: finiteNumber(
        initialBalanceSheet.startingPortfolioUsd,
        defaultInitialBalanceSheet.startingPortfolioUsd
      ),
      privateEquityUnits,
    },
    policies: {
      liquidReservePolicy: liquidReservePolicyIds.has(policies.liquidReservePolicy)
        ? policies.liquidReservePolicy
        : defaultPolicies.liquidReservePolicy,
      checkingFloorUsd: finiteNumber(policies.checkingFloorUsd, defaultPolicies.checkingFloorUsd),
      checkingSaleAmountUsd: positiveNumber(policies.checkingSaleAmountUsd, defaultPolicies.checkingSaleAmountUsd),
      privateEquitySalePolicy: PRIVATE_EQUITY_SALE_POLICY_IDS.has(policies.privateEquitySalePolicy)
        ? policies.privateEquitySalePolicy
        : defaultPolicies.privateEquitySalePolicy,
      privateEquityLiquidNetWorthFloorUsd: finiteNumber(
        policies.privateEquityLiquidNetWorthFloorUsd,
        defaultPolicies.privateEquityLiquidNetWorthFloorUsd
      ),
      privateEquityTenderSaleAmountUsd: positiveNumber(
        policies.privateEquityTenderSaleAmountUsd,
        defaultPolicies.privateEquityTenderSaleAmountUsd
      ),
    },
  };
}

export function normalizeScenarioSetInput(input, bootstrap) {
  const defaultInput = createDefaultScenarioSetInput(bootstrap);
  const existingIds = new Set();
  const scenariosSource =
    Array.isArray(input?.scenarios) && input.scenarios.length > 0 ? input.scenarios : defaultInput.scenarios;
  const scenarios = scenariosSource.map((scenario, index) =>
    normalizeScenarioInput(scenario, bootstrap, index, existingIds)
  );
  const horizonMonths = Math.max(1, ...scenarios.map((scenario) => Math.ceil(scenario.timeline.holdYears * 12)));
  return {
    title: typeof input?.title === "string" && input.title.trim() ? input.title.trim() : defaultInput.title,
    marketRequest: {
      ...defaultInput.marketRequest,
      ...(input?.marketRequest && typeof input.marketRequest === "object" ? input.marketRequest : {}),
      horizonMonths,
      rolloutCount: positiveNumber(input?.marketRequest?.rolloutCount, defaultInput.marketRequest.rolloutCount),
      seed: nullableInteger(input?.marketRequest?.seed, defaultInput.marketRequest.seed),
    },
    reportSpec: normalizeReportSpec(input?.reportSpec),
    scenarios,
  };
}

function normalizeReportSpec(reportSpec) {
  const source = reportSpec && typeof reportSpec === "object" ? reportSpec : {};
  return {
    percentiles: Array.isArray(source.percentiles) ? source.percentiles : DEFAULT_REPORT_SPEC.percentiles,
    includeMonthlyColumns: Boolean(source.includeMonthlyColumns ?? DEFAULT_REPORT_SPEC.includeMonthlyColumns),
  };
}

function agentsByRole(bootstrap) {
  const agents = bootstrap?.agents ?? [];
  const primary = agents.find((a) => a.role === "primary_owner");
  const partner = agents.find((a) => a.role === "equity_building_occupant") ?? null;
  return { primary, partner };
}

function occupancyModeForScenario(scenario) {
  const occupancyAndRental = scenario.occupancyAndRental;
  if (occupancyAndRental.ownerResidenceMode === "other_owned_property") {
    return "owner_lives_in_other_owned_property";
  }
  if (occupancyAndRental.ownerResidenceMode === "rental_elsewhere") {
    return "owner_rents_elsewhere";
  }
  if (occupancyAndRental.rentalUsePolicy === "rent_whole_property") {
    return "no_owner_occupancy";
  }
  return "owner_lives_in_property";
}

function rentalModeForScenario(scenario) {
  const { rentalUsePolicy } = scenario.occupancyAndRental;
  if (rentalUsePolicy === "rent_rooms_while_owner_lives_there") {
    return "rent_rooms_while_owner_lives_there";
  }
  if (rentalUsePolicy === "rent_whole_property") {
    return "rent_whole_property";
  }
  return "not_rented";
}

function scenarioPolicies(scenario, bootstrap) {
  const { identity, actorsAndOwnership, policies: scenarioPolicyInputs } = scenario;
  const { primary, partner } = agentsByRole(bootstrap);
  const policies = [];
  if (actorsAndOwnership.actorPolicy === "owner_plus_partner" && partner) {
    policies.push({
      policyId: "partner_equity_accrual",
      policyType: "partner_equity_accrual",
      actorId: partner.actorId,
      enabled: identity.enabled,
      baseMonthlyPaymentUsd: actorsAndOwnership.partnerPaymentMonthlyUsd,
    });
  }
  if (scenarioPolicyInputs.liquidReservePolicy === "checking_floor_sp500") {
    policies.push({
      policyId: "checking_floor",
      policyType: "checking_floor_sell_public_stock",
      actorId: primary.actorId,
      enabled: true,
      floorUsd: scenarioPolicyInputs.checkingFloorUsd,
      saleAmountUsd: scenarioPolicyInputs.checkingSaleAmountUsd,
    });
  }
  if (scenarioPolicyInputs.privateEquitySalePolicy === "liquid_net_worth_floor") {
    policies.push({
      policyId: "private_equity_liquid_floor_sale",
      policyType: "private_equity_sale",
      actorId: primary.actorId,
      enabled: true,
      proceedsDestination: "generic_sp500_stock",
      saleRule: {
        saleRuleType: "liquid_net_worth_floor",
        minLiquidNetWorthUsd: scenarioPolicyInputs.privateEquityLiquidNetWorthFloorUsd,
        saleAmountUsd: scenarioPolicyInputs.privateEquityTenderSaleAmountUsd,
      },
    });
  }
  return policies;
}

function scenarioActors(scenario, bootstrap) {
  const { actorPolicy } = scenario.actorsAndOwnership;
  const { primary, partner } = agentsByRole(bootstrap);
  const actors = [
    {
      actorId: primary.actorId,
      label: primary.label,
      role: primary.role,
    },
  ];
  if (actorPolicy === "owner_plus_partner" && partner) {
    actors.push({
      actorId: partner.actorId,
      label: partner.label,
      role: partner.role,
    });
  }
  return actors;
}

function scenarioEvents(scenario, property, bootstrap) {
  const { propertyId } = scenario.propertyAndLocation;
  const { downPaymentPct } = scenario.financing;
  const { primary } = agentsByRole(bootstrap);
  const loanAmountUsd = Math.max(0, (property?.priceUsd ?? 0) * (1 - downPaymentPct / 100));
  const events = [
    {
      eventId: "purchase",
      eventType: "property_purchase",
      monthIndex: 0,
      actorId: primary.actorId,
      propertyId,
      amountUsd: property?.priceUsd ?? 0,
      description: "Property purchase at scenario start.",
      hoaMonthlyUsd: property?.hoaMonthlyUsd ?? 0,
    },
    {
      eventId: "mortgage",
      eventType: "mortgage_origination",
      monthIndex: 0,
      actorId: primary.actorId,
      propertyId,
      amountUsd: loanAmountUsd,
      description: "Mortgage originated at scenario start.",
    },
  ];
  return events;
}

function scenarioBalanceSheet(scenario, bootstrap) {
  const initialBalanceSheet = scenario.initialBalanceSheet;
  const { primary } = agentsByRole(bootstrap);
  const privateEquityUnits = Math.max(0, finiteNumber(initialBalanceSheet.privateEquityUnits, 0));
  const privateEquityValueUsd = privateEquityValueUsdForUnits(bootstrap, privateEquityUnits);
  const assets = [
    {
      assetId: "sp500",
      assetType: "generic_sp500_stock",
      ownerActorId: primary.actorId,
      valueUsd: initialBalanceSheet.startingPortfolioUsd,
      costBasisUsd: initialBalanceSheet.startingPortfolioUsd,
    },
  ];
  if (privateEquityValueUsd > 0 || privateEquityUnits > 0) {
    assets.push({
      assetId: "private_equity_private",
      assetType: "private_equity",
      ownerActorId: primary.actorId,
      valueUsd: privateEquityValueUsd,
      units: privateEquityUnits,
      costBasisUsd: 0,
    });
  }
  return {
    accounts: [
      {
        accountId: "checking",
        accountType: "checking",
        ownerActorId: primary.actorId,
        balanceUsd: initialBalanceSheet.initialCheckingUsd,
      },
    ],
    assets,
    liabilities: [],
  };
}

function scenarioToBackendScenario(scenario, bootstrap) {
  const { identity, propertyAndLocation, timeline, financing, occupancyAndRental, propertyAssumptions, taxAccounting } =
    scenario;
  const property = propertyById(bootstrap, propertyAndLocation.propertyId);
  const holdMonths = Math.ceil(timeline.holdYears * 12);
  const rentalMode = rentalModeForScenario(scenario);
  const rentEstimate = finiteNumber(property?.rentEstimateUsd, 0);
  const beds = Math.max(1, finiteNumber(property?.beds, 1));
  return {
    scenarioId: identity.scenarioId,
    label: identity.label,
    enabled: identity.enabled,
    color: identity.color,
    actors: scenarioActors(scenario, bootstrap),
    events: scenarioEvents(scenario, property, bootstrap),
    policies: scenarioPolicies(scenario, bootstrap),
    propertySelection: {
      propertyId: propertyAndLocation.propertyId,
    },
    financing: {
      financingMode: financing.financingMode,
      downPaymentPct: financing.downPaymentPct,
      mortgageRatePct: financing.customMortgageRate,
      mortgageTermYears: financing.customMortgageTermYears,
      creditScore: financing.creditScore,
    },
    occupancyPlan: {
      occupancyMode: occupancyModeForScenario(scenario),
      ownerResidencePropertyId:
        occupancyAndRental.ownerResidenceMode === "selected_property" ? propertyAndLocation.propertyId : null,
      startMonth: 0,
      endMonth: occupancyAndRental.rentalUsePolicy === "rent_whole_property" ? 0 : holdMonths,
    },
    rentalPlan: {
      rentalMode,
      startMonth: rentalMode === "not_rented" ? null : 0,
      endMonth: rentalMode === "not_rented" ? null : holdMonths,
      monthlyRentUsd: rentalMode === "rent_whole_property" ? rentEstimate : null,
      roomsRented:
        rentalMode === "rent_rooms_while_owner_lives_there"
          ? Math.min(Math.max(0, occupancyAndRental.roomsRentedWhileLiving), Math.max(0, beds - 1))
          : 0,
      roomRentMonthlyUsd:
        rentalMode === "rent_rooms_while_owner_lives_there" ? occupancyAndRental.roomRentMonthlyUsd : null,
      vacancyPct: occupancyAndRental.vacancyPct,
      roomVacancyPct: occupancyAndRental.roomVacancyPct,
      managementFeePct: occupancyAndRental.managementFeePct,
      leasingFeePct: occupancyAndRental.leasingFeePct,
    },
    transactionCosts: {
      closingCostBuyPct: taxAccounting.closingCostBuyPct,
      closingCostSellPct: taxAccounting.closingCostSellPct,
    },
    propertyAssumptions: {
      insuranceAnnualUsd: propertyAssumptions.insuranceAnnualUsd,
      maintenancePct: propertyAssumptions.maintenancePct,
      depreciableBasisPct: propertyAssumptions.depreciableBasisPct,
    },
    initialBalanceSheet: scenarioBalanceSheet(scenario, bootstrap),
  };
}

export function scenarioSetInputToRequest(input, bootstrap) {
  const normalized = normalizeScenarioSetInput(input, bootstrap);
  return {
    scenarioSetId: "augur_futures_explorer",
    title: normalized.title,
    marketRequest: normalized.marketRequest,
    reportSpec: normalized.reportSpec,
    scenarios: normalized.scenarios.map((scenario) => scenarioToBackendScenario(scenario, bootstrap)),
  };
}

function serializableScenario(scenario) {
  const {
    identity,
    propertyAndLocation,
    actorsAndOwnership,
    timeline,
    financing,
    occupancyAndRental,
    propertyAssumptions,
    taxAccounting,
    initialBalanceSheet,
    policies,
  } = scenario;
  return {
    identity: {
      scenarioId: identity.scenarioId,
      label: identity.label,
      enabled: identity.enabled,
      color: identity.color,
    },
    propertyAndLocation: {
      propertyId: propertyAndLocation.propertyId,
    },
    actorsAndOwnership: {
      actorPolicy: actorsAndOwnership.actorPolicy,
      partnerPaymentMonthlyUsd: actorsAndOwnership.partnerPaymentMonthlyUsd,
    },
    timeline: {
      holdYears: timeline.holdYears,
    },
    financing: {
      financingMode: financing.financingMode,
      downPaymentPct: financing.downPaymentPct,
      ...(financing.financingMode === "custom"
        ? {
            customMortgageRate: financing.customMortgageRate,
            customMortgageTermYears: financing.customMortgageTermYears,
          }
        : {}),
      creditScore: financing.creditScore,
    },
    occupancyAndRental: {
      ownerResidenceMode: occupancyAndRental.ownerResidenceMode,
      rentalUsePolicy: occupancyAndRental.rentalUsePolicy,
      vacancyPct: occupancyAndRental.vacancyPct,
      managementFeePct: occupancyAndRental.managementFeePct,
      leasingFeePct: occupancyAndRental.leasingFeePct,
      roomsRentedWhileLiving: occupancyAndRental.roomsRentedWhileLiving,
      roomRentMonthlyUsd: occupancyAndRental.roomRentMonthlyUsd,
      roomVacancyPct: occupancyAndRental.roomVacancyPct,
    },
    propertyAssumptions: {
      maintenancePct: propertyAssumptions.maintenancePct,
      insuranceAnnualUsd: propertyAssumptions.insuranceAnnualUsd,
      depreciableBasisPct: propertyAssumptions.depreciableBasisPct,
    },
    taxAccounting: {
      closingCostBuyPct: taxAccounting.closingCostBuyPct,
      closingCostSellPct: taxAccounting.closingCostSellPct,
    },
    initialBalanceSheet: {
      initialCheckingUsd: initialBalanceSheet.initialCheckingUsd,
      startingPortfolioUsd: initialBalanceSheet.startingPortfolioUsd,
      privateEquityUnits: initialBalanceSheet.privateEquityUnits,
    },
    policies: {
      liquidReservePolicy: policies.liquidReservePolicy,
      checkingFloorUsd: policies.checkingFloorUsd,
      checkingSaleAmountUsd: policies.checkingSaleAmountUsd,
      privateEquitySalePolicy: policies.privateEquitySalePolicy,
      privateEquityLiquidNetWorthFloorUsd: policies.privateEquityLiquidNetWorthFloorUsd,
      privateEquityTenderSaleAmountUsd: policies.privateEquityTenderSaleAmountUsd,
    },
  };
}

function serializableScenarioSetInput(input) {
  return {
    title: input.title,
    marketRequest: input.marketRequest,
    reportSpec: normalizeReportSpec(input.reportSpec),
    scenarios: (input.scenarios ?? []).map(serializableScenario),
  };
}

function bytesToBase64Url(bytes) {
  if (typeof Buffer !== "undefined") {
    return Buffer.from(bytes).toString("base64").replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  }
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function base64UrlToBytes(value) {
  const base64 = value
    .replaceAll("-", "+")
    .replaceAll("_", "/")
    .padEnd(Math.ceil(value.length / 4) * 4, "=");
  if (typeof Buffer !== "undefined") {
    return Uint8Array.from(Buffer.from(base64, "base64"));
  }
  const binary = atob(base64);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

export function encodeScenarioSetUrlState(input) {
  const payload = {
    version: URL_STATE_VERSION,
    scenario_set_input: decamelizeObjectKeys(serializableScenarioSetInput(input)),
  };
  return bytesToBase64Url(new TextEncoder().encode(JSON.stringify(payload)));
}

export function decodeScenarioSetUrlState(value) {
  if (!value) return null;
  const payload = JSON.parse(new TextDecoder().decode(base64UrlToBytes(value)));
  if (payload?.version !== URL_STATE_VERSION) {
    throw new Error(`Unsupported augur scenario URL state version: ${payload?.version ?? "<missing>"}`);
  }
  if (!payload.scenario_set_input || typeof payload.scenario_set_input !== "object") {
    throw new Error("Augur scenario URL state is missing scenario_set_input");
  }
  // Intentionally not parsed against zBrowserScenarioSetInput: the URL stores
  // a sparse-overrides payload (only fields the user changed away from
  // bootstrap defaults), and the model marks every section's fields as
  // required. normalizeScenarioSetInput below materializes the full shape
  // from URL state + bootstrap defaults and that's what consumers see.
  return camelizeObjectKeys(payload.scenario_set_input);
}

export function scenarioSetInputFromUrlSearch(search) {
  const params = new URLSearchParams(search);
  return decodeScenarioSetUrlState(params.get("state"));
}

export function searchWithScenarioSetInput(search, input) {
  const params = new URLSearchParams(search);
  params.set("state", encodeScenarioSetUrlState(input));
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}
