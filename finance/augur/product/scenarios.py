"""Build sim scenarios from product `ScenarioKey` payloads."""

from __future__ import annotations

from dataclasses import dataclass

from more_itertools import one

from finance.augur.api.config import Config, LocationConfig
from finance.augur.api.portfolio import PortfolioConfig
from finance.augur.api.wire import ActorRole, Property
from finance.augur.model.series import InflationKey, IssuerId, LocationId, RentKey
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.product.wire import (
    CapitalImprovementEventWire,
    CashFinancing,
    FundingPolicy,
    MortgageFinancing,
    PropertyLifecycleEventWire,
    PropertyPurchase,
    PropertySaleEventWire,
    RentalIncomePlan,
    ScenarioKey,
    SetPrimaryResidenceEventWire,
    SetRentedFractionEventWire,
)
from finance.augur.sim.locations import Location
from finance.augur.sim.pricing import OccupancyMode, insurance_rate, maintenance_rate
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    CapitalImprovementEvent,
    FilingStatus,
    FixedAmount,
    HarvestPolicy,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing as SimMortgageFinancing,
    MortgageInterestDeductionPolicy,
    ObligationType,
    PrimaryResidenceAssignment,
    PrivateEquityTenderPolicy,
    PropertyLifecycleEvent,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    Scenario,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SeriesIndexedAmount,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
    TransferDeductionCategory,
)

PRIMARY_ACCOUNT_ID = "checking"
SPEND_SINK_AGENT_ID = "spend_sink"
SPEND_SINK_ACCOUNT_ID = "checking"
SPEND_OBLIGATION_ID = "monthly_spend"
LANDLORD_AGENT_ID = "landlord"
LANDLORD_ACCOUNT_ID = "checking"
RENT_OBLIGATION_ID = "outside_rent"
TAX_AUTHORITY_AGENT_ID = "tax_authority"
TAX_AUTHORITY_ACCOUNT_ID = "checking"
PROPERTY_SELLER_AGENT_ID = "property_seller"
PROPERTY_SELLER_ACCOUNT_ID = "checking"
MORTGAGE_LENDER_AGENT_ID = "mortgage_lender"
MORTGAGE_LENDER_ACCOUNT_ID = "checking"
HOA_AGENT_ID = "hoa"
HOA_ACCOUNT_ID = "checking"
HOA_OBLIGATION_ID = "hoa_dues"
INSURER_AGENT_ID = "insurer"
INSURER_ACCOUNT_ID = "checking"
INSURANCE_OBLIGATION_ID = "homeowners_insurance"
MAINTENANCE_VENDOR_AGENT_ID = "maintenance_vendor"
MAINTENANCE_VENDOR_ACCOUNT_ID = "checking"
MAINTENANCE_OBLIGATION_ID = "property_maintenance"
TENANT_AGENT_ID = "tenant"
TENANT_ACCOUNT_ID = "checking"
RENTAL_INCOME_CAUSE_ID = "rental_income"
PROPERTY_MANAGEMENT_AGENT_ID = "property_management_agency"
PROPERTY_MANAGEMENT_ACCOUNT_ID = "checking"
MANAGEMENT_FEE_CAUSE_ID = "management_fee"
LEASING_FEE_CAUSE_ID = "leasing_fee"


def sim_locations_from_config(locations: tuple[LocationConfig, ...]) -> dict[str, Location]:
    return {
        loc.location_id: Location(
            location_id=loc.location_id,
            display_name=loc.label,
            jurisdiction_ids=[str(r) for r in loc.local_regulation.default_tax_regimes],
            annual_property_tax_rate=float(loc.local_regulation.property_tax_annual_pct) / 100.0,
            annual_special_assessment_usd=float(loc.local_regulation.special_assessment_annual_usd),
        )
        for loc in locations
    }


def resolve_primary_agent_id(augur_config: Config) -> str:
    return one(agent.actor_id for agent in augur_config.agents if agent.role == ActorRole.PRIMARY_OWNER)


def initial_lots_from_portfolio(portfolio: PortfolioConfig, *, primary_agent_id: str) -> tuple[InitialLot, ...]:
    lots = portfolio.to_initial_lots()
    unsupported_owner_ids = sorted({lot.agent_id for lot in lots if lot.agent_id != primary_agent_id})
    if unsupported_owner_ids:
        raise ValueError(
            "product portfolio projection only supports holding lots owned by the primary agent; "
            f"got owner agent ids {unsupported_owner_ids}"
        )
    return lots


def asset_label_by_series_id(portfolio: PortfolioConfig) -> dict[str, str]:
    # Keyed by the sim-frame wire id (matching the `asset_id` column on decoded sim event
    # frames) so sim events can be labeled; the wire id is derived from the typed `asset`.
    return {
        position.asset.wire_id: f"{position.label or position.display_symbol} ({position.display_symbol})"
        for position in portfolio.holdings
    }


def required_private_equity_issuers(initial_lots: tuple[InitialLot, ...]) -> frozenset[IssuerId]:
    return frozenset(lot.asset.issuer_id for lot in initial_lots if isinstance(lot.asset, PrivateEquityAssetKey))


def build_scenario(
    scenario_key: ScenarioKey,
    *,
    primary_agent_id: str,
    initial_cash_usd: float,
    initial_lots: tuple[InitialLot, ...],
    properties_by_id: dict[str, Property],
    harvest_policies: tuple[HarvestPolicy, ...] = (),
) -> Scenario:
    horizon_months = int(scenario_key.horizon_months)
    end_month = horizon_months - 1

    agents = [
        Agent(agent_id=primary_agent_id),
        Agent(agent_id=SPEND_SINK_AGENT_ID),
        Agent(agent_id=TAX_AUTHORITY_AGENT_ID),
    ]
    initial_cash = [
        InitialAccountBalance(agent_id=primary_agent_id, account_id=PRIMARY_ACCOUNT_ID, balance_usd=initial_cash_usd),
        InitialAccountBalance(agent_id=SPEND_SINK_AGENT_ID, account_id=SPEND_SINK_ACCOUNT_ID, balance_usd=0.0),
        InitialAccountBalance(agent_id=TAX_AUTHORITY_AGENT_ID, account_id=TAX_AUTHORITY_ACCOUNT_ID, balance_usd=0.0),
    ]
    recurring_obligations = [
        RecurringObligation(
            start_month=0,
            end_month=end_month,
            obligation_id=SPEND_OBLIGATION_ID,
            obligation_type=ObligationType.CASH_SPEND,
            agent_id=primary_agent_id,
            from_account_id=PRIMARY_ACCOUNT_ID,
            to_agent_id=SPEND_SINK_AGENT_ID,
            to_account_id=SPEND_SINK_ACCOUNT_ID,
            amount_due_usd=_monthly_spend_amount(scenario_key),
        )
    ]
    recurring_transfers: list[RecurringTransfer] = []
    scheduled_transfers: list[ScheduledTransfer] = []

    if scenario_key.monthly_rent_usd > 0:
        assert scenario_key.rental_location_id is not None  # wire validator guarantees
        agents.append(Agent(agent_id=LANDLORD_AGENT_ID))
        initial_cash.append(
            InitialAccountBalance(agent_id=LANDLORD_AGENT_ID, account_id=LANDLORD_ACCOUNT_ID, balance_usd=0.0)
        )
        recurring_obligations.append(
            RecurringObligation(
                start_month=0,
                end_month=end_month,
                obligation_id=RENT_OBLIGATION_ID,
                obligation_type=ObligationType.OUTSIDE_RENT,
                agent_id=primary_agent_id,
                from_account_id=PRIMARY_ACCOUNT_ID,
                to_agent_id=LANDLORD_AGENT_ID,
                to_account_id=LANDLORD_ACCOUNT_ID,
                amount_due_usd=SeriesIndexedAmount(
                    base_amount_usd=float(scenario_key.monthly_rent_usd),
                    series=RentKey(location_id=LocationId(scenario_key.rental_location_id)),
                    adjustment_period_months=12,
                ),
            )
        )

    scheduled_property_purchases: list[ScheduledPropertyPurchase] = []
    initial_primary_residences: list[PrimaryResidenceAssignment] = []
    primary_residence_events: list[SetPrimaryResidenceEvent] = []
    property_lifecycle_events: list[PropertyLifecycleEvent] = []
    property_tax_policies: list[PropertyTaxPolicy] = []
    mortgage_interest_deduction_policies: list[MortgageInterestDeductionPolicy] = []
    scheduled_property_cashflows: list[ScheduledPropertyCashflow] = []
    recurring_property_cashflows: list[RecurringPropertyCashflow] = []
    if scenario_key.property_purchase is not None:
        property_ = properties_by_id[scenario_key.property_purchase.property_id]
        agents.append(Agent(agent_id=PROPERTY_SELLER_AGENT_ID))
        initial_cash.append(
            InitialAccountBalance(
                agent_id=PROPERTY_SELLER_AGENT_ID, account_id=PROPERTY_SELLER_ACCOUNT_ID, balance_usd=0.0
            )
        )
        mortgage = _sim_mortgage_for(scenario_key.property_purchase, property_)
        if mortgage is not None:
            agents.append(Agent(agent_id=MORTGAGE_LENDER_AGENT_ID))
            initial_cash.append(
                InitialAccountBalance(
                    agent_id=MORTGAGE_LENDER_AGENT_ID, account_id=MORTGAGE_LENDER_ACCOUNT_ID, balance_usd=0.0
                )
            )
            if scenario_key.property_purchase.is_primary_residence:
                mortgage_interest_deduction_policies.append(
                    MortgageInterestDeductionPolicy(liability_id=mortgage.liability_id, owner_agent_id=primary_agent_id)
                )
        scheduled_property_purchases.append(
            _sim_property_purchase(
                scenario_key.property_purchase, property_, primary_agent_id=primary_agent_id, mortgage=mortgage
            )
        )
        if scenario_key.property_purchase.is_primary_residence:
            initial_primary_residences.append(
                PrimaryResidenceAssignment(agent_id=primary_agent_id, property_id=property_.id)
            )
        for event in scenario_key.property_purchase.lifecycle_events:
            if isinstance(event, SetPrimaryResidenceEventWire):
                primary_residence_events.append(
                    SetPrimaryResidenceEvent(
                        month=int(event.month),
                        agent_id=primary_agent_id,
                        property_id=property_.id if event.is_primary_residence else None,
                    )
                )
            else:
                property_lifecycle_events.append(_sim_lifecycle_event(event, property_id=property_.id))
        property_tax_policies.append(
            PropertyTaxPolicy(
                property_id=property_.id,
                owner_agent_id=primary_agent_id,
                from_account_id=PRIMARY_ACCOUNT_ID,
                tax_authority_agent_id=TAX_AUTHORITY_AGENT_ID,
                tax_authority_account_id=TAX_AUTHORITY_ACCOUNT_ID,
                annual_tax_rate=None,  # fall back to location YAML
                start_month=0,
                end_month=end_month,
            )
        )
        initial_occupancy_mode, initial_rented_fraction = _initial_occupancy(scenario_key.property_purchase)
        # Schedule E for property expenses: the rented fraction of HOA / insurance / maintenance /
        # property tax is deductible against rental income. When these obligations carry
        # `property_id`, the sim reads the runtime rented fraction at settlement time so
        # mid-horizon stop/restart events resize the Schedule E share.
        property_deduction_category, property_deductible_fraction = _schedule_e_split(initial_rented_fraction)
        if property_.hoa_monthly_usd > 0:
            agents.append(Agent(agent_id=HOA_AGENT_ID))
            initial_cash.append(
                InitialAccountBalance(agent_id=HOA_AGENT_ID, account_id=HOA_ACCOUNT_ID, balance_usd=0.0)
            )
            recurring_obligations.append(
                RecurringObligation(
                    start_month=0,
                    end_month=end_month,
                    obligation_id=HOA_OBLIGATION_ID,
                    obligation_type=ObligationType.HOA_DUES,
                    agent_id=primary_agent_id,
                    from_account_id=PRIMARY_ACCOUNT_ID,
                    to_agent_id=HOA_AGENT_ID,
                    to_account_id=HOA_ACCOUNT_ID,
                    amount_due_usd=SeriesIndexedAmount(
                        base_amount_usd=float(property_.hoa_monthly_usd),
                        series=InflationKey(),
                        adjustment_period_months=1,
                    ),
                    deduction_category=property_deduction_category,
                    deductible_fraction=property_deductible_fraction,
                    property_id=property_.id,
                )
            )
        if scenario_key.annual_insurance_pct > 0:
            agents.append(Agent(agent_id=INSURER_AGENT_ID))
            initial_cash.append(
                InitialAccountBalance(agent_id=INSURER_AGENT_ID, account_id=INSURER_ACCOUNT_ID, balance_usd=0.0)
            )
            effective_insurance_pct = insurance_rate(
                base_annual_pct=float(scenario_key.annual_insurance_pct),
                occupancy_mode=initial_occupancy_mode,
                rented_fraction=initial_rented_fraction,
            )
            monthly_insurance_usd = effective_insurance_pct / 100.0 * float(property_.price_usd) / 12.0
            recurring_obligations.append(
                RecurringObligation(
                    start_month=0,
                    end_month=end_month,
                    obligation_id=INSURANCE_OBLIGATION_ID,
                    obligation_type=ObligationType.HOMEOWNERS_INSURANCE,
                    agent_id=primary_agent_id,
                    from_account_id=PRIMARY_ACCOUNT_ID,
                    to_agent_id=INSURER_AGENT_ID,
                    to_account_id=INSURER_ACCOUNT_ID,
                    amount_due_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_insurance_usd, series=InflationKey(), adjustment_period_months=1
                    ),
                    deduction_category=property_deduction_category,
                    deductible_fraction=property_deductible_fraction,
                    property_id=property_.id,
                )
            )
        if scenario_key.annual_maintenance_pct > 0:
            agents.append(Agent(agent_id=MAINTENANCE_VENDOR_AGENT_ID))
            initial_cash.append(
                InitialAccountBalance(
                    agent_id=MAINTENANCE_VENDOR_AGENT_ID, account_id=MAINTENANCE_VENDOR_ACCOUNT_ID, balance_usd=0.0
                )
            )
            effective_maintenance_pct = maintenance_rate(
                base_annual_pct=float(scenario_key.annual_maintenance_pct),
                occupancy_mode=initial_occupancy_mode,
                rented_fraction=initial_rented_fraction,
            )
            monthly_maintenance_usd = effective_maintenance_pct / 100.0 * float(property_.price_usd) / 12.0
            recurring_obligations.append(
                RecurringObligation(
                    start_month=0,
                    end_month=end_month,
                    obligation_id=MAINTENANCE_OBLIGATION_ID,
                    obligation_type=ObligationType.PROPERTY_MAINTENANCE,
                    agent_id=primary_agent_id,
                    from_account_id=PRIMARY_ACCOUNT_ID,
                    to_agent_id=MAINTENANCE_VENDOR_AGENT_ID,
                    to_account_id=MAINTENANCE_VENDOR_ACCOUNT_ID,
                    amount_due_usd=SeriesIndexedAmount(
                        base_amount_usd=monthly_maintenance_usd, series=InflationKey(), adjustment_period_months=1
                    ),
                    deduction_category=property_deduction_category,
                    deductible_fraction=property_deductible_fraction,
                    property_id=property_.id,
                )
            )
        rental_wiring = _wire_landlord_rental(
            scenario_key.property_purchase,
            property_=property_,
            primary_agent_id=primary_agent_id,
            horizon_months=horizon_months,
        )
        agents.extend(rental_wiring.agents)
        initial_cash.extend(rental_wiring.initial_cash)
        recurring_property_cashflows.extend(rental_wiring.recurring_property_cashflows)
        scheduled_property_cashflows.extend(rental_wiring.scheduled_property_cashflows)

    private_equity_tender_policies = _build_private_equity_tender_policies(
        scenario_key=scenario_key, initial_lots=initial_lots, primary_agent_id=primary_agent_id
    )
    return Scenario(
        agents=agents,
        initial_lots=list(initial_lots),
        initial_cash=initial_cash,
        recurring_obligations=recurring_obligations,
        recurring_transfers=recurring_transfers,
        scheduled_transfers=scheduled_transfers,
        recurring_property_cashflows=recurring_property_cashflows,
        scheduled_property_cashflows=scheduled_property_cashflows,
        scheduled_property_purchases=scheduled_property_purchases,
        initial_primary_residences=initial_primary_residences,
        primary_residence_events=primary_residence_events,
        property_lifecycle_events=property_lifecycle_events,
        property_tax_policies=property_tax_policies,
        mortgage_interest_deduction_policies=mortgage_interest_deduction_policies,
        private_equity_tender_policies=private_equity_tender_policies,
        tax_profiles=[
            TaxProfile(
                agent_id=primary_agent_id,
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id=TAX_AUTHORITY_AGENT_ID,
                payment_account_id=PRIMARY_ACCOUNT_ID,
                tax_authority_account_id=TAX_AUTHORITY_ACCOUNT_ID,
            )
        ],
        target_allocation_policies=_target_allocation_policies_from_funding_policy(
            scenario_key.funding_policy, primary_agent_id=primary_agent_id, initial_lots=initial_lots
        ),
        harvest_policies=list(harvest_policies),
        horizon_months=horizon_months,
    )


def _resolve_monthly_rent(rental: RentalIncomePlan, *, property_: Property) -> float:
    """Resolve full-property gross monthly rent for a landlord rental.

    User-supplied `full_property_monthly_rent_usd` wins. Otherwise fall back to the
    deployment's `Property.rent_estimate_usd`. The caller scales the resulting full-property
    rent by `fraction_rented` and vacancy. If neither rent source is set, reject the request —
    the deployment is missing data the scenario needs.
    """

    if rental.full_property_monthly_rent_usd is not None:
        return float(rental.full_property_monthly_rent_usd)
    if property_.rent_estimate_usd is None:
        raise ValueError(
            f"property {property_.id!r} has no rent_estimate_usd and the scenario did not supply "
            "full_property_monthly_rent_usd; one or the other is required to model rental income"
        )
    return float(property_.rent_estimate_usd)


def _schedule_e_split(rented_fraction: float) -> tuple[TransferDeductionCategory | None, float]:
    """Compute the (`deduction_category`, `deductible_fraction`) pair for a property expense.

    Rented fraction > 0 → property expenses route a `rented_fraction` share to Schedule E
    against rental income; fraction = 0 (pure owner-occupied) → no Schedule E deduction
    (mortgage interest still flows to MID via the existing MID policy; SALT applies as
    today). The Phase 2 follow-ups in `augur/sim/TODO.md` track scaling MID/SALT
    themselves by (1 - rented_fraction); this helper only handles the Schedule E side.
    """

    if rented_fraction <= 0.0:
        return (None, 0.0)
    return ("ordinary", float(rented_fraction))


def _sim_lifecycle_event(event: PropertyLifecycleEventWire, *, property_id: str) -> PropertyLifecycleEvent:
    """Translate one wire lifecycle event to its sim-side equivalent.

    Wire variants and sim variants are kept separate because the wire variants are scoped
    to a specific PropertyPurchase (so they don't carry property_id), while sim variants
    are a flat list with explicit property_id. Beyond that the shapes match. Dispatch is
    by `isinstance` over the Pydantic discriminated union.
    """

    month = int(event.month)
    if isinstance(event, SetRentedFractionEventWire):
        return SetRentedFractionEvent(
            month=month, property_id=property_id, rented_fraction=float(event.rented_fraction)
        )
    if isinstance(event, SetPrimaryResidenceEventWire):
        raise TypeError("SetPrimaryResidenceEventWire is lowered separately from property lifecycle events")
    if isinstance(event, CapitalImprovementEventWire):
        return CapitalImprovementEvent(
            month=month, property_id=property_id, amount_usd=float(event.amount_usd), description=event.description
        )
    if isinstance(event, PropertySaleEventWire):
        return PropertySaleEvent(month=month, property_id=property_id, closing_cost_pct=float(event.closing_cost_pct))
    raise TypeError(f"unknown PropertyLifecycleEventWire variant: {type(event).__name__}")


def _initial_occupancy(purchase: PropertyPurchase) -> tuple[OccupancyMode, float]:
    """Initial-month (occupancy_mode, rented_fraction) implied by the purchase.

    Expense pricing still uses the initial state. Section 121 primary-residence use is now
    tracked separately by sim-side agent assignment events.
    """

    if purchase.initial_rental is None:
        return OccupancyMode.OWNER_OCCUPIED if purchase.is_primary_residence else OccupancyMode.OFF, 0.0
    fraction = float(purchase.initial_rental.fraction_rented)
    if fraction >= 1.0:
        return OccupancyMode.RENTED_FULL, 1.0
    return OccupancyMode.RENTED_PARTIAL, fraction


@dataclass(frozen=True)
class LandlordRentalWiring:
    """Per-property landlord rental wiring produced by `_wire_landlord_rental`. Caller
    extends its parallel `agents`/`initial_cash`/property cashflow lists with these
    fields — one merge site per property, instead of mutating caller-owned lists
    threaded through the helper as kwargs."""

    agents: tuple[Agent, ...]
    initial_cash: tuple[InitialAccountBalance, ...]
    recurring_property_cashflows: tuple[RecurringPropertyCashflow, ...]
    scheduled_property_cashflows: tuple[ScheduledPropertyCashflow, ...]


_EMPTY_LANDLORD_RENTAL_WIRING = LandlordRentalWiring(
    agents=(), initial_cash=(), recurring_property_cashflows=(), scheduled_property_cashflows=()
)


@dataclass(frozen=True)
class _RentalCashflowSegment:
    start_month: int
    end_month: int
    fraction_rented: float


@dataclass(frozen=True)
class _RentalCashflowTerms:
    base_monthly_rent: float
    vacancy_multiplier: float


def _wire_landlord_rental(
    purchase: PropertyPurchase, *, property_: Property, primary_agent_id: str, horizon_months: int
) -> LandlordRentalWiring:
    """Wire up tenant→owner rent + owner→agency management/leasing fees.

    Tenant rent and agency fees follow the property's effective rented-fraction timeline:
    the initial fraction comes from `initial_rental`, later `set_rented_fraction` events
    resize/stop/restart the cashflows, and sale stops rental cashflows in the sale month.
    Tenant rent is gross-of-management but net-of-vacancy (vacancy is "lost income", not
    paid to anyone). Management fee is a separate outbound transfer to the agency. Leasing
    fee fires when each rental segment starts and every `avg_tenancy_months` while active.
    """

    terms = _rental_cashflow_terms(purchase, property_=property_)
    if terms is None:
        return _EMPTY_LANDLORD_RENTAL_WIRING
    rent_series = RentKey(location_id=LocationId(property_.location_id))
    base_monthly_rent = terms.base_monthly_rent
    vacancy_multiplier = terms.vacancy_multiplier
    rental_segments = _rental_cashflow_segments(purchase, horizon_months=horizon_months)
    if not rental_segments:
        return _EMPTY_LANDLORD_RENTAL_WIRING

    agents: list[Agent] = [Agent(agent_id=TENANT_AGENT_ID)]
    initial_cash: list[InitialAccountBalance] = [
        InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id=TENANT_ACCOUNT_ID, balance_usd=0.0)
    ]
    recurring_property_cashflows: list[RecurringPropertyCashflow] = []
    scheduled_property_cashflows: list[ScheduledPropertyCashflow] = []
    for segment in rental_segments:
        leased_monthly_rent = base_monthly_rent * segment.fraction_rented
        base_monthly_collected = leased_monthly_rent * vacancy_multiplier
        recurring_property_cashflows.append(
            RecurringPropertyCashflow(
                start_month=segment.start_month,
                end_month=segment.end_month,
                property_id=property_.id,
                cause_id=f"{RENTAL_INCOME_CAUSE_ID}:{property_.id}",
                from_agent_id=TENANT_AGENT_ID,
                from_account_id=TENANT_ACCOUNT_ID,
                to_agent_id=primary_agent_id,
                to_account_id=PRIMARY_ACCOUNT_ID,
                amount_usd=SeriesIndexedAmount(
                    base_amount_usd=base_monthly_collected, series=rent_series, adjustment_period_months=12
                ),
                # Rental income is ordinary income (taxed at owner's marginal bracket).
                # §469 passive-loss limitation is explicitly deferred per the plan.
                income_category=ORDINARY_INCOME,
            )
        )

    management = purchase.rental_management
    if management is not None:
        agents.append(Agent(agent_id=PROPERTY_MANAGEMENT_AGENT_ID))
        initial_cash.append(
            InitialAccountBalance(
                agent_id=PROPERTY_MANAGEMENT_AGENT_ID, account_id=PROPERTY_MANAGEMENT_ACCOUNT_ID, balance_usd=0.0
            )
        )
        management_fee_fraction = float(management.management_fee_pct) / 100.0
        if management_fee_fraction > 0:
            for segment in rental_segments:
                base_monthly_collected = base_monthly_rent * segment.fraction_rented * vacancy_multiplier
                recurring_property_cashflows.append(
                    RecurringPropertyCashflow(
                        start_month=segment.start_month,
                        end_month=segment.end_month,
                        property_id=property_.id,
                        cause_id=f"{MANAGEMENT_FEE_CAUSE_ID}:{property_.id}",
                        from_agent_id=primary_agent_id,
                        from_account_id=PRIMARY_ACCOUNT_ID,
                        to_agent_id=PROPERTY_MANAGEMENT_AGENT_ID,
                        to_account_id=PROPERTY_MANAGEMENT_ACCOUNT_ID,
                        amount_usd=SeriesIndexedAmount(
                            base_amount_usd=base_monthly_collected * management_fee_fraction,
                            series=rent_series,
                            adjustment_period_months=12,
                        ),
                        # Management fee is a Schedule E deduction against rental income.
                        deduction_category="ordinary",
                    )
                )
        leasing_fee_months_val = float(management.leasing_fee_months)
        if leasing_fee_months_val > 0:
            for segment in rental_segments:
                leasing_fee_base = base_monthly_rent * segment.fraction_rented * leasing_fee_months_val
                scheduled_property_cashflows.extend(
                    ScheduledPropertyCashflow(
                        month=fire_month,
                        property_id=property_.id,
                        cause_id=f"{LEASING_FEE_CAUSE_ID}:{property_.id}:m{fire_month}",
                        from_agent_id=primary_agent_id,
                        from_account_id=PRIMARY_ACCOUNT_ID,
                        to_agent_id=PROPERTY_MANAGEMENT_AGENT_ID,
                        to_account_id=PROPERTY_MANAGEMENT_ACCOUNT_ID,
                        amount_usd=SeriesIndexedAmount(
                            base_amount_usd=leasing_fee_base, series=rent_series, adjustment_period_months=12
                        ),
                        # Leasing fee is a Schedule E deduction against rental income.
                        deduction_category="ordinary",
                    )
                    for fire_month in range(
                        segment.start_month, segment.end_month + 1, int(management.avg_tenancy_months)
                    )
                )
    return LandlordRentalWiring(
        agents=tuple(agents),
        initial_cash=tuple(initial_cash),
        recurring_property_cashflows=tuple(recurring_property_cashflows),
        scheduled_property_cashflows=tuple(scheduled_property_cashflows),
    )


def _rental_cashflow_terms(purchase: PropertyPurchase, *, property_: Property) -> _RentalCashflowTerms | None:
    if purchase.initial_rental is not None:
        return _RentalCashflowTerms(
            base_monthly_rent=_resolve_monthly_rent(purchase.initial_rental, property_=property_),
            vacancy_multiplier=1.0 - float(purchase.initial_rental.vacancy_pct),
        )
    if not _has_positive_rented_fraction_event(purchase):
        return None
    if property_.rent_estimate_usd is None:
        raise ValueError(
            f"property {property_.id!r} has a future rented-fraction event but no rent_estimate_usd; "
            "set initial_rental.full_property_monthly_rent_usd so product lowering knows full-property rent"
        )
    return _RentalCashflowTerms(
        base_monthly_rent=float(property_.rent_estimate_usd),
        vacancy_multiplier=1.0 - float(RentalIncomePlan().vacancy_pct),
    )


def _has_positive_rented_fraction_event(purchase: PropertyPurchase) -> bool:
    return any(
        isinstance(event, SetRentedFractionEventWire) and float(event.rented_fraction) > 0.0
        for event in purchase.lifecycle_events
    )


def _rental_cashflow_segments(purchase: PropertyPurchase, *, horizon_months: int) -> tuple[_RentalCashflowSegment, ...]:
    end_month = horizon_months - 1
    current_start = 0
    current_fraction = _initial_rented_fraction(purchase)
    segments: list[_RentalCashflowSegment] = []
    for event in sorted(
        (
            event
            for event in purchase.lifecycle_events
            if isinstance(event, SetRentedFractionEventWire | PropertySaleEventWire)
        ),
        key=lambda event: int(event.month),
    ):
        event_month = int(event.month)
        segment_end = min(event_month - 1, end_month)
        if current_start <= segment_end and current_fraction > 0:
            segments.append(
                _RentalCashflowSegment(
                    start_month=current_start, end_month=segment_end, fraction_rented=current_fraction
                )
            )
        if event_month > end_month:
            return tuple(segments)
        if isinstance(event, PropertySaleEventWire):
            return tuple(segments)
        current_start = event_month
        current_fraction = float(event.rented_fraction)
    if current_start <= end_month and current_fraction > 0:
        segments.append(
            _RentalCashflowSegment(start_month=current_start, end_month=end_month, fraction_rented=current_fraction)
        )
    return tuple(segments)


def _sim_mortgage_for(purchase: PropertyPurchase, property_: Property) -> SimMortgageFinancing | None:
    if isinstance(purchase.financing, CashFinancing):
        return None
    assert isinstance(purchase.financing, MortgageFinancing)
    principal = float(property_.price_usd) * (1.0 - purchase.financing.down_payment_pct / 100.0)
    return SimMortgageFinancing(
        liability_id=f"{property_.id}_mortgage",
        lender_agent_id=MORTGAGE_LENDER_AGENT_ID,
        lender_account_id=MORTGAGE_LENDER_ACCOUNT_ID,
        principal_usd=principal,
        annual_interest_rate=purchase.financing.annual_rate_pct / 100.0,
        term_months=purchase.financing.term_months,
    )


def _sim_property_purchase(
    purchase: PropertyPurchase, property_: Property, *, primary_agent_id: str, mortgage: SimMortgageFinancing | None
) -> ScheduledPropertyPurchase:
    purchase_price = float(property_.price_usd)
    if isinstance(purchase.financing, CashFinancing):
        down_payment = purchase_price
    else:
        down_payment = purchase_price * purchase.financing.down_payment_pct / 100.0
    rented_fraction = _initial_rented_fraction(purchase)
    return ScheduledPropertyPurchase(
        month=0,
        cause_id=f"{property_.id}_purchase",
        property_id=property_.id,
        location_id=property_.location_id,
        buyer_agent_id=primary_agent_id,
        buyer_account_id=PRIMARY_ACCOUNT_ID,
        seller_agent_id=PROPERTY_SELLER_AGENT_ID,
        seller_account_id=PROPERTY_SELLER_ACCOUNT_ID,
        purchase_price_usd=purchase_price,
        down_payment_usd=down_payment,
        buyer_closing_cost_usd=purchase_price * float(purchase.closing_cost_pct) / 100.0,
        mortgage=mortgage,
        rented_fraction=rented_fraction,
        # The wire schema doesn't yet expose this knob; we use the sim default (0.20) until
        # the deployment-config / property-record story lands. See augur/sim/TODO.md.
    )


def _initial_rented_fraction(purchase: PropertyPurchase) -> float:
    return float(purchase.initial_rental.fraction_rented) if purchase.initial_rental is not None else 0.0


def _monthly_spend_amount(scenario_key: ScenarioKey) -> float | SeriesIndexedAmount:
    if scenario_key.spend_index == "inflation":
        return SeriesIndexedAmount(
            base_amount_usd=float(scenario_key.monthly_spend_usd), series=InflationKey(), adjustment_period_months=1
        )
    if scenario_key.spend_index == "none":
        return float(scenario_key.monthly_spend_usd)
    raise ValueError(f"unsupported spend_index: {scenario_key.spend_index!r}")


def _target_allocation_policies_from_funding_policy(
    funding_policy: FundingPolicy, *, primary_agent_id: str, initial_lots: tuple[InitialLot, ...]
) -> list[TargetAllocationPolicy]:
    """Lower the wire's cash band + weights to the sim's target-allocation policy.

    Zero-weight sleeves are DROPPED rather than passed down: zero is the UI's way of saying
    "outside the target", and the sim's `SleeveTarget` requires a positive weight because a zero
    there would be a divisor in the water level. A weight naming nothing held is dropped too, so
    a saved target can outlive the position it mentions.

    No sleeves left means no policy at all, which preserves the old `sell_order=[]` behaviour:
    the owner never auto-sells and an unaffordable obligation is ruin.
    """

    sellable = [lot.asset for lot in initial_lots if not isinstance(lot.asset, PrivateEquityAssetKey)]
    held_by_symbol = {asset.symbol: asset for asset in sellable}
    sleeves = [
        SleeveTarget(asset=held_by_symbol[sleeve.symbol], weight=sleeve.weight)
        for sleeve in funding_policy.sleeve_weights
        if sleeve.weight > 0 and sleeve.symbol in held_by_symbol
    ]
    if not sleeves:
        return []
    targeted = {sleeve.asset for sleeve in sleeves}
    return [
        TargetAllocationPolicy(
            agent_id=primary_agent_id,
            account_id=PRIMARY_ACCOUNT_ID,
            source_account_ids=tuple(dict.fromkeys(lot.account_id for lot in initial_lots if lot.asset in targeted)),
            sleeves=sleeves,
            cash_floor_usd=_cash_buffer_amount(
                float(funding_policy.cash_floor_usd), index_to_inflation=funding_policy.cash_band_index_to_inflation
            ),
            cash_ceiling_usd=_cash_buffer_amount(
                float(funding_policy.cash_ceiling_usd), index_to_inflation=funding_policy.cash_band_index_to_inflation
            ),
            cause_id_prefix="product_funding_sale",
        )
    ]


def _cash_buffer_amount(usd: float, *, index_to_inflation: bool) -> FixedAmount | SeriesIndexedAmount | float:
    """Translate a UI scalar + index flag into the sim `AmountSpec`. Indexed buffers track CPI
    monthly (period=1) so the real-terms target stays constant; nominal buffers stay as the raw
    float."""

    if not index_to_inflation or usd <= 0:
        return usd
    return SeriesIndexedAmount(base_amount_usd=usd, series=InflationKey(), adjustment_period_months=1)


def _build_private_equity_tender_policies(
    *, scenario_key: ScenarioKey, initial_lots: tuple[InitialLot, ...], primary_agent_id: str
) -> list[PrivateEquityTenderPolicy]:
    """Build the sim `PrivateEquityTenderPolicy` list from the wire's pe_tender_policy.

    A single policy targets the primary agent. It is emitted whenever the user holds PE,
    even with a zero floor: the floor only controls voluntary tender/public-market sales,
    while exogenous forced-sale/recovery events still need owner/proceeds routing.
    """

    holds_pe = any(isinstance(lot.asset, PrivateEquityAssetKey) for lot in initial_lots)
    floor_usd = float(scenario_key.pe_tender_policy.liquid_net_worth_floor_usd)
    if not holds_pe:
        return []
    if floor_usd > 0 and scenario_key.pe_tender_policy.index_floor_to_inflation:
        floor: FixedAmount | SeriesIndexedAmount = SeriesIndexedAmount(
            base_amount_usd=floor_usd, series=InflationKey(), adjustment_period_months=1
        )
    else:
        floor = FixedAmount(amount_usd=floor_usd)
    return [
        PrivateEquityTenderPolicy(
            owner_agent_id=primary_agent_id, proceeds_account_id=PRIMARY_ACCOUNT_ID, liquid_net_worth_floor=floor
        )
    ]
