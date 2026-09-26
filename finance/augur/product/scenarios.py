"""Declare the app's situation from a product `ScenarioKey`: lowered once, composed onto one world per path."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import partial

from more_itertools import duplicates_everseen, one

from finance.augur.api.config import Config, LocationConfig, SecurityDistributionConfig
from finance.augur.api.portfolio import PortfolioConfig
from finance.augur.api.wire import ActorRole, Property
from finance.augur.model.asset_key import PrivateEquityAssetKey
from finance.augur.model.series import InflationKey, IssuerId, LevelSeriesKey, LocationId, RentKey, SecurityKey
from finance.augur.policy.cash_band_household import (
    BandBound,
    CashBandHousehold,
    CpiIndexed,
    ManagedSleeve,
    SecuritySleeve,
    Sleeve,
)
from finance.augur.policy.funding import ClaimPayer
from finance.augur.product.wire import (
    CapitalImprovementEventWire,
    CashFinancing,
    FundingPolicy,
    ManagedSleeveWeight,
    MortgageFinancing,
    PropertyLifecycleEventWire,
    PropertyPurchase,
    PropertySaleEventWire,
    RentalIncomePlan,
    ScenarioKey,
    SetPrimaryResidenceEventWire,
    SetRentedFractionEventWire,
    SpendIndex,
)
from finance.augur.sim.bills import Biller
from finance.augur.sim.compiler.execution import (
    compile_accounts,
    compile_bond,
    compile_distribution,
    compile_holding_pools,
    compile_housing,
    compile_interest_deduction,
    compile_jurisdictions,
    compile_locations,
    compile_lots,
    compile_private_equity_series,
    compile_property_cashflow,
    compile_property_tax,
    compile_recurring_obligation,
    compile_recurring_property_cashflow,
    compile_series,
    compile_tender_policy,
    compile_tlh_portfolio,
)
from finance.augur.sim.compiler.series import level_series_demand
from finance.augur.sim.compiler.tax import PreparedTaxProfile, compile_income_sources, compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, round_currency_amount
from finance.augur.sim.ids import AccountId, AgentId, AssetId, PropertyId
from finance.augur.sim.locations import Location
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedBond,
    PreparedDistribution,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedLot,
    PreparedPropertyCashflow,
    PreparedRecurringObligation,
    PreparedRecurringPropertyCashflow,
    PreparedSeries,
    PreparedTlhPortfolio,
    _MortgageInterestDeduction,
    _PropertyTax,
    _TenderPolicy,
)
from finance.augur.sim.pricing import OccupancyMode, insurance_rate, maintenance_rate
from finance.augur.sim.property import Housing
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    BondHolding,
    CapitalImprovementEvent,
    Currency,
    DistributionTaxSlice,
    FilingStatus,
    FixedAmount,
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
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    SecurityDistribution,
    SeriesIndexedAmount,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
    TaxProfile,
    TlhPortfolioSpec,
    TransferDeductionCategory,
    TransferIncomeCategory,
)
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

PRIMARY_ACCOUNT_ID = AccountId("checking")
SPEND_SINK_AGENT_ID = AgentId("spend_sink")
SPEND_SINK_ACCOUNT_ID = AccountId("checking")
SPEND_OBLIGATION_ID = "monthly_spend"
LANDLORD_AGENT_ID = AgentId("landlord")
LANDLORD_ACCOUNT_ID = AccountId("checking")
RENT_OBLIGATION_ID = "outside_rent"
TAX_AUTHORITY_AGENT_ID = AgentId("tax_authority")
TAX_AUTHORITY_ACCOUNT_ID = AccountId("checking")
PROPERTY_SELLER_AGENT_ID = AgentId("property_seller")
PROPERTY_SELLER_ACCOUNT_ID = AccountId("checking")
MORTGAGE_LENDER_AGENT_ID = AgentId("mortgage_lender")
MORTGAGE_LENDER_ACCOUNT_ID = AccountId("checking")
HOA_AGENT_ID = AgentId("hoa")
HOA_ACCOUNT_ID = AccountId("checking")
HOA_OBLIGATION_ID = "hoa_dues"
INSURER_AGENT_ID = AgentId("insurer")
INSURER_ACCOUNT_ID = AccountId("checking")
INSURANCE_OBLIGATION_ID = "homeowners_insurance"
MAINTENANCE_VENDOR_AGENT_ID = AgentId("maintenance_vendor")
MAINTENANCE_VENDOR_ACCOUNT_ID = AccountId("checking")
MAINTENANCE_OBLIGATION_ID = "property_maintenance"
TENANT_AGENT_ID = AgentId("tenant")
TENANT_ACCOUNT_ID = AccountId("checking")
RENTAL_INCOME_CAUSE_ID = "rental_income"
PROPERTY_MANAGEMENT_AGENT_ID = AgentId("property_management_agency")
PROPERTY_MANAGEMENT_ACCOUNT_ID = AccountId("checking")
MANAGEMENT_FEE_CAUSE_ID = "management_fee"
LEASING_FEE_CAUSE_ID = "leasing_fee"


def _amount(value: object) -> Decimal:
    """Make an existing exact/configured product amount explicit before sim validation."""

    return value if isinstance(value, Decimal) else Decimal(str(value))


def sim_locations_from_config(locations: tuple[LocationConfig, ...]) -> dict[LocationId, Location]:
    return {
        loc.location_id: Location(
            location_id=loc.location_id,
            display_name=loc.label,
            jurisdiction_ids=[str(r) for r in loc.local_regulation.default_tax_regimes],
            annual_property_tax_rate=float(loc.local_regulation.property_tax_annual_pct) / 100.0,
            annual_special_assessment=_amount(loc.local_regulation.special_assessment_annual),
        )
        for loc in locations
    }


def resolve_primary_agent_id(augur_config: Config) -> AgentId:
    return one(agent.actor_id for agent in augur_config.agents if agent.role == ActorRole.PRIMARY_OWNER)


def initial_lots_from_portfolio(portfolio: PortfolioConfig, *, primary_agent_id: AgentId) -> tuple[InitialLot, ...]:
    lots = portfolio.to_initial_lots()
    unsupported_owner_ids = sorted({lot.agent_id for lot in lots if lot.agent_id != primary_agent_id})
    if unsupported_owner_ids:
        raise ValueError(
            "product portfolio projection only supports holding lots owned by the primary agent; "
            f"got owner agent ids {unsupported_owner_ids}"
        )
    return lots


def initial_bonds_from_portfolio(portfolio: PortfolioConfig, *, primary_agent_id: AgentId) -> tuple[BondHolding, ...]:
    bonds = portfolio.to_initial_bonds(coupon_account_id=PRIMARY_ACCOUNT_ID)
    unsupported_owner_ids = sorted({bond.agent_id for bond in bonds if bond.agent_id != primary_agent_id})
    if unsupported_owner_ids:
        raise ValueError(
            "product portfolio projection only supports bonds owned by the primary agent; "
            f"got owner agent ids {unsupported_owner_ids}"
        )
    return bonds


def security_distributions_from_portfolio(
    portfolio: PortfolioConfig,
    declarations: tuple[SecurityDistributionConfig, ...],
    *,
    tlh_portfolios: tuple[TlhPortfolioSpec, ...],
    primary_agent_id: AgentId,
) -> tuple[SecurityDistribution, ...]:
    """Payout specs for every held pool of a security the deployment declares as distributing.

    The two halves meet here and nowhere else: the deployment's list says WHAT a fund is made
    of (a fact about the instrument), the portfolio says WHERE it is held, and this function
    knows the product's cash topology well enough to name the destination. A TLH portfolio is
    a pool of its own: the sim pays it on the portfolio's value, not on units.
    """

    tax_character_by_symbol = {
        declaration.symbol: tuple(
            DistributionTaxSlice(fraction=share.fraction, issuer_jurisdiction_id=share.issuer_jurisdiction_id)
            for share in declaration.tax_character
        )
        for declaration in declarations
    }
    distributions = portfolio.to_security_distributions(
        tax_character_by_symbol=tax_character_by_symbol, payout_account_id=PRIMARY_ACCOUNT_ID
    ) + tuple(
        SecurityDistribution(
            asset=managed.asset,
            agent_id=managed.owner_agent_id,
            holding_account_id=managed.account_id,
            to_account_id=PRIMARY_ACCOUNT_ID,
            tax_character=tax_character_by_symbol[managed.asset.symbol],
        )
        for managed in tlh_portfolios
        if isinstance(managed.asset, SecurityKey) and managed.asset.symbol in tax_character_by_symbol
    )
    unsupported_owner_ids = sorted({d.agent_id for d in distributions if d.agent_id != primary_agent_id})
    if unsupported_owner_ids:
        raise ValueError(
            "product portfolio projection only supports distributions on holdings owned by the "
            f"primary agent; got owner agent ids {unsupported_owner_ids}"
        )
    return distributions


def asset_label_by_series_id(portfolio: PortfolioConfig) -> dict[str, str]:
    # Keyed by the sim-frame wire id (matching the `asset_id` column on decoded sim event
    # frames) so sim events can be labeled; the wire id is derived from the typed `asset`.
    # TLH portfolios need no entry: their events name the portfolio, never an asset.
    return {
        position.asset.wire_id: f"{position.label or position.display_symbol} ({position.display_symbol})"
        for position in portfolio.holdings
    }


@dataclass(frozen=True)
class Home:
    """The purchased property: its tables, the authority that taxes it and the cashflows it carries."""

    housing: Housing
    property_tax: _PropertyTax
    location: PreparedLocation
    # Claimed only on a financed primary residence.
    interest_deduction: _MortgageInterestDeduction | None
    cashflows: tuple[PreparedPropertyCashflow | PreparedRecurringPropertyCashflow, ...]


@dataclass(frozen=True)
class Situation:
    """What every path of one request shares, lowered once; `compose` declares it onto each path's world."""

    currency: Currency
    horizon_months: int
    # A fresh household per path: it keeps the path's CPI history and purchase identities.
    household: Callable[[], CashBandHousehold | ClaimPayer]
    # Every series the declarations read, which is what the request samples.
    level_series: tuple[LevelSeriesKey, ...]
    private_equity_issuers: frozenset[IssuerId]
    jurisdictions: tuple[PreparedJurisdiction, ...]
    income_sources: tuple[TransferIncomeCategory, ...]
    accounts: tuple[PreparedAccount, ...]
    tax_profile: PreparedTaxProfile
    pools: tuple[PreparedHoldingPool, ...]
    lots: tuple[PreparedLot, ...]
    tlh_portfolios: tuple[PreparedTlhPortfolio, ...]
    bonds: tuple[PreparedBond, ...]
    home: Home | None
    distributions: tuple[PreparedDistribution, ...]
    tender_policy: _TenderPolicy | None
    obligations: tuple[PreparedRecurringObligation, ...]


def build_situation(
    scenario_key: ScenarioKey,
    *,
    primary_agent_id: AgentId,
    initial_cash: Decimal,
    initial_lots: tuple[InitialLot, ...],
    properties_by_id: dict[PropertyId, Property],
    locations: Mapping[LocationId, Location],
    initial_bonds: tuple[BondHolding, ...] = (),
    security_distributions: tuple[SecurityDistribution, ...] = (),
    tlh_portfolios: tuple[TlhPortfolioSpec, ...] = (),
) -> Situation:
    horizon_months = int(scenario_key.horizon_months)
    end_month = horizon_months - 1
    currency = Currency(code=scenario_key.currency_code, quantum=scenario_key.currency_quantum)
    quantum = currency.quantum
    currency_quantum = scenario_key.currency_quantum

    initial_balances = [
        InitialAccountBalance(agent_id=primary_agent_id, account_id=PRIMARY_ACCOUNT_ID, balance=initial_cash),
        InitialAccountBalance(agent_id=SPEND_SINK_AGENT_ID, account_id=SPEND_SINK_ACCOUNT_ID, balance=0),
        InitialAccountBalance(agent_id=TAX_AUTHORITY_AGENT_ID, account_id=TAX_AUTHORITY_ACCOUNT_ID, balance=0),
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
            amount_due=_monthly_spend_amount(scenario_key),
        )
    ]

    if scenario_key.monthly_rent > 0:
        assert scenario_key.rental_location_id is not None  # wire validator guarantees
        initial_balances.append(
            InitialAccountBalance(agent_id=LANDLORD_AGENT_ID, account_id=LANDLORD_ACCOUNT_ID, balance=0)
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
                amount_due=SeriesIndexedAmount(
                    base_amount=scenario_key.monthly_rent,
                    series=RentKey(location_id=LocationId(scenario_key.rental_location_id)),
                    adjustment_period_months=12,
                ),
            )
        )

    scheduled_property_purchases: list[ScheduledPropertyPurchase] = []
    scheduled_property_cashflows: list[ScheduledPropertyCashflow] = []
    recurring_property_cashflows: list[RecurringPropertyCashflow] = []
    home = None
    if scenario_key.property_purchase is not None:
        property_ = properties_by_id[scenario_key.property_purchase.property_id]
        initial_balances.append(
            InitialAccountBalance(agent_id=PROPERTY_SELLER_AGENT_ID, account_id=PROPERTY_SELLER_ACCOUNT_ID, balance=0)
        )
        mortgage = _sim_mortgage_for(scenario_key.property_purchase, property_, currency_quantum=currency_quantum)
        interest_deduction = None
        if mortgage is not None:
            initial_balances.append(
                InitialAccountBalance(
                    agent_id=MORTGAGE_LENDER_AGENT_ID, account_id=MORTGAGE_LENDER_ACCOUNT_ID, balance=0
                )
            )
            if scenario_key.property_purchase.is_primary_residence:
                interest_deduction = compile_interest_deduction(
                    MortgageInterestDeductionPolicy(
                        liability_id=mortgage.liability_id, owner_agent_id=primary_agent_id
                    ),
                    quantum=quantum,
                )
        scheduled_property_purchases.append(
            _sim_property_purchase(
                scenario_key.property_purchase,
                property_,
                primary_agent_id=primary_agent_id,
                mortgage=mortgage,
                currency_quantum=currency_quantum,
            )
        )
        initial_primary_residences = (
            [PrimaryResidenceAssignment(agent_id=primary_agent_id, property_id=property_.id)]
            if scenario_key.property_purchase.is_primary_residence
            else []
        )
        primary_residence_events: list[SetPrimaryResidenceEvent] = []
        property_lifecycle_events: list[PropertyLifecycleEvent] = []
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
        expense_wiring = _wire_property_expenses(
            scenario_key,
            property_=property_,
            primary_agent_id=primary_agent_id,
            horizon_months=horizon_months,
            currency_quantum=currency_quantum,
        )
        initial_balances.extend(expense_wiring.initial_cash)
        recurring_obligations.extend(expense_wiring.recurring_obligations)
        rental_wiring = _wire_landlord_rental(
            scenario_key.property_purchase,
            property_=property_,
            primary_agent_id=primary_agent_id,
            horizon_months=horizon_months,
            currency_quantum=currency_quantum,
        )
        initial_balances.extend(rental_wiring.initial_cash)
        recurring_property_cashflows.extend(rental_wiring.recurring_property_cashflows)
        scheduled_property_cashflows.extend(rental_wiring.scheduled_property_cashflows)
        home = Home(
            housing=compile_housing(
                purchases=scheduled_property_purchases,
                initial_residences=initial_primary_residences,
                residence_events=primary_residence_events,
                lifecycle_events=property_lifecycle_events,
                quantum=quantum,
            ),
            property_tax=compile_property_tax(
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
            ),
            location=one(compile_locations(scheduled_property_purchases, locations, quantum=quantum)),
            interest_deduction=interest_deduction,
            cashflows=(
                *(compile_property_cashflow(cashflow, quantum=quantum) for cashflow in scheduled_property_cashflows),
                *(
                    compile_recurring_property_cashflow(cashflow, quantum=quantum)
                    for cashflow in recurring_property_cashflows
                ),
            ),
        )

    tender_policies = _build_private_equity_tender_policies(
        scenario_key=scenario_key, initial_lots=initial_lots, primary_agent_id=primary_agent_id
    )
    household, band = _funding_household(
        scenario_key.funding_policy,
        primary_agent_id=primary_agent_id,
        initial_lots=initial_lots,
        tlh_portfolios=tlh_portfolios,
        quantum=quantum,
    )
    # The funding policy sells a pool's lots oldest first, so a pool may not hold two lots bought the same month.
    bought = [(lot.agent_id, lot.account_id, lot.asset.wire_id, lot.purchase_month_index) for lot in initial_lots]
    if len(set(bought)) != len(bought):
        raise ValueError(
            f"duplicate initial lot purchase months for FIFO pool(s): {sorted(set(duplicates_everseen(bought)))}"
        )
    profile = TaxProfile(
        agent_id=primary_agent_id,
        filing_status=FilingStatus.SINGLE,
        jurisdiction_ids=["federal_us", "california"],
        tax_authority_agent_id=TAX_AUTHORITY_AGENT_ID,
        payment_account_id=PRIMARY_ACCOUNT_ID,
        tax_authority_account_id=TAX_AUTHORITY_ACCOUNT_ID,
    )
    jurisdictions = load_jurisdictions_for([profile])
    return Situation(
        currency=currency,
        horizon_months=horizon_months,
        household=household,
        level_series=level_series_demand(
            lots=initial_lots,
            tlh_portfolios=tlh_portfolios,
            bonds=initial_bonds,
            distributions=security_distributions,
            amounts=(
                *(cashflow.amount for cashflow in scheduled_property_cashflows),
                *(cashflow.amount for cashflow in recurring_property_cashflows),
                *(obligation.amount_due for obligation in recurring_obligations),
                # Both band bounds, not just the floor: the ceiling is the refill target a raise is
                # sized to, so an indexed ceiling needs its series sampled.
                *band,
            ),
            tender_policies=tender_policies,
            purchases=scheduled_property_purchases,
        ),
        private_equity_issuers=frozenset(
            lot.asset.issuer_id for lot in initial_lots if isinstance(lot.asset, PrivateEquityAssetKey)
        ),
        jurisdictions=compile_jurisdictions(jurisdictions, bonds=initial_bonds, distributions=security_distributions),
        income_sources=compile_income_sources(
            flows=(*scheduled_property_cashflows, *recurring_property_cashflows),
            bonds=initial_bonds,
            distributions=security_distributions,
        ),
        accounts=compile_accounts(initial_balances, quantum=quantum),
        tax_profile=compile_profile(profile, jurisdictions, quantum=quantum),
        pools=compile_holding_pools(lots=initial_lots),
        lots=compile_lots(initial_lots, quantum=quantum),
        tlh_portfolios=tuple(compile_tlh_portfolio(portfolio, quantum=quantum) for portfolio in tlh_portfolios),
        bonds=tuple(compile_bond(bond, quantum=quantum) for bond in initial_bonds),
        home=home,
        distributions=tuple(compile_distribution(distribution) for distribution in security_distributions),
        tender_policy=None if not tender_policies else compile_tender_policy(one(tender_policies), quantum=quantum),
        obligations=tuple(
            compile_recurring_obligation(obligation, quantum=quantum) for obligation in recurring_obligations
        ),
    )


def paths(situation: Situation, sampled: ExternalSeriesContext, *, rollout_count: int) -> tuple[PreparedSeries, ...]:
    """The sampled paths as the integer series a world reads: levels, then each held issuer's protocol."""
    return (
        *compile_series(
            sampled,
            rollout_count=rollout_count,
            horizon_months=situation.horizon_months,
            currency_quantum=situation.currency.quantum,
        ),
        *compile_private_equity_series(
            sorted(situation.private_equity_issuers),
            sampled.private_equity,
            rollout_count=rollout_count,
            horizon_months=situation.horizon_months,
            quantum=situation.currency.quantum,
        ),
    )


def compose(situation: Situation, market: MarketPath) -> World:
    """One path's world: the household's books, holdings, home and counterparties, and the household tracked."""
    world = World(
        market,
        horizon_months=situation.horizon_months,
        income_sources=situation.income_sources,
        jurisdictions=situation.jurisdictions,
    )
    for account in situation.accounts:
        world.declare_account(account)
    world.track(TaxAuthority(situation.tax_profile))
    if situation.home is not None and situation.home.interest_deduction is not None:
        world.declare_deduction(situation.home.interest_deduction)
    for pool in situation.pools:
        world.declare_pool(pool)
    for lot in situation.lots:
        world.hold(lot)
    for portfolio in situation.tlh_portfolios:
        world.declare_portfolio(portfolio)
    for bond in situation.bonds:
        world.hold(bond)
    if situation.home is not None:
        world.declare_housing(situation.home.housing, (situation.home.property_tax,), (situation.home.location,))
    for distribution in situation.distributions:
        world.declare_distribution(distribution)
    if situation.tender_policy is not None:
        world.declare_tender_policy(situation.tender_policy)
    if situation.home is not None:
        for flow in situation.home.cashflows:
            world.declare_flow(flow)
    for obligation in situation.obligations:
        world.track(Biller(obligation))
    household = situation.household()
    if isinstance(household, CashBandHousehold):
        household.check(world)
    world.track(household)
    return world


def _resolve_monthly_rent(rental: RentalIncomePlan, *, property_: Property) -> Decimal:
    """Resolve full-property gross monthly rent for a landlord rental.

    User-supplied `full_property_monthly_rent` wins. Otherwise fall back to the
    deployment's `Property.rent_estimate`. The caller scales the resulting full-property
    rent by `fraction_rented` and vacancy. If neither rent source is set, reject the request —
    the deployment is missing data the scenario needs.
    """

    if rental.full_property_monthly_rent is not None:
        return rental.full_property_monthly_rent
    if property_.rent_estimate is None:
        raise ValueError(
            f"property {property_.id!r} has no rent_estimate and the scenario did not supply "
            "full_property_monthly_rent; one or the other is required to model rental income"
        )
    return _amount(property_.rent_estimate)


def _schedule_e_split(rented_fraction: float) -> tuple[TransferDeductionCategory | None, float]:
    """Compute the (`deduction_category`, `deductible_fraction`) pair for a property expense.

    Rented fraction > 0 → property expenses route a `rented_fraction` share to Schedule E
    against rental income; fraction = 0 (pure owner-occupied) → no Schedule E deduction
    (mortgage interest and SALT are handled separately); this helper only handles
    the Schedule E expense share.
    """

    if rented_fraction <= 0.0:
        return (None, 0.0)
    return ("ordinary", float(rented_fraction))


def _sim_lifecycle_event(event: PropertyLifecycleEventWire, *, property_id: PropertyId) -> PropertyLifecycleEvent:
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
            month=month, property_id=property_id, amount=event.amount, description=event.description
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
class PropertyExpenseWiring:
    """Payees and obligations for recurring property expenses."""

    initial_cash: tuple[InitialAccountBalance, ...]
    recurring_obligations: tuple[RecurringObligation, ...]


def _wire_property_expenses(
    scenario_key: ScenarioKey,
    *,
    property_: Property,
    primary_agent_id: AgentId,
    horizon_months: int,
    currency_quantum: Decimal,
) -> PropertyExpenseWiring:
    """Wire HOA, insurance, and maintenance payees for one purchased property.

    The returned tuple fields are immutable so the caller can merge this property's wiring
    into the scenario's parallel collections without handing mutable lists into the helper.
    Property tax remains a policy rather than a payee obligation and is wired by the caller.
    """

    purchase = scenario_key.property_purchase
    assert purchase is not None
    end_month = horizon_months - 1
    initial_occupancy_mode, initial_rented_fraction = _initial_occupancy(purchase)
    # When these obligations carry `property_id`, the sim reads the runtime rented fraction at
    # settlement time so mid-horizon stop/restart events resize the Schedule E share.
    property_deduction_category, property_deductible_fraction = _schedule_e_split(initial_rented_fraction)
    initial_cash: list[InitialAccountBalance] = []
    recurring_obligations: list[RecurringObligation] = []
    if property_.hoa_monthly > 0:
        initial_cash.append(InitialAccountBalance(agent_id=HOA_AGENT_ID, account_id=HOA_ACCOUNT_ID, balance=0))
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
                amount_due=SeriesIndexedAmount(
                    base_amount=_amount(property_.hoa_monthly), series=InflationKey(), adjustment_period_months=1
                ),
                deduction_category=property_deduction_category,
                deductible_fraction=property_deductible_fraction,
                property_id=property_.id,
            )
        )
    if scenario_key.annual_insurance_pct > 0:
        initial_cash.append(InitialAccountBalance(agent_id=INSURER_AGENT_ID, account_id=INSURER_ACCOUNT_ID, balance=0))
        effective_insurance_pct = insurance_rate(
            base_annual_pct=float(scenario_key.annual_insurance_pct),
            occupancy_mode=initial_occupancy_mode,
            rented_fraction=initial_rented_fraction,
        )
        monthly_insurance = round_currency_amount(
            _amount(property_.price) * _amount(effective_insurance_pct) / Decimal(100 * 12), quantum=currency_quantum
        )
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
                amount_due=SeriesIndexedAmount(
                    base_amount=monthly_insurance, series=InflationKey(), adjustment_period_months=1
                ),
                deduction_category=property_deduction_category,
                deductible_fraction=property_deductible_fraction,
                property_id=property_.id,
            )
        )
    if scenario_key.annual_maintenance_pct > 0:
        initial_cash.append(
            InitialAccountBalance(
                agent_id=MAINTENANCE_VENDOR_AGENT_ID, account_id=MAINTENANCE_VENDOR_ACCOUNT_ID, balance=0
            )
        )
        effective_maintenance_pct = maintenance_rate(
            base_annual_pct=float(scenario_key.annual_maintenance_pct),
            occupancy_mode=initial_occupancy_mode,
            rented_fraction=initial_rented_fraction,
        )
        monthly_maintenance = round_currency_amount(
            _amount(property_.price) * _amount(effective_maintenance_pct) / Decimal(100 * 12), quantum=currency_quantum
        )
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
                amount_due=SeriesIndexedAmount(
                    base_amount=monthly_maintenance, series=InflationKey(), adjustment_period_months=1
                ),
                deduction_category=property_deduction_category,
                deductible_fraction=property_deductible_fraction,
                property_id=property_.id,
            )
        )
    return PropertyExpenseWiring(initial_cash=tuple(initial_cash), recurring_obligations=tuple(recurring_obligations))


@dataclass(frozen=True)
class LandlordRentalWiring:
    """Per-property landlord rental wiring produced by `_wire_landlord_rental`. Caller
    extends its parallel `initial_cash`/property cashflow lists with these
    fields — one merge site per property, instead of mutating caller-owned lists
    threaded through the helper as kwargs."""

    initial_cash: tuple[InitialAccountBalance, ...]
    recurring_property_cashflows: tuple[RecurringPropertyCashflow, ...]
    scheduled_property_cashflows: tuple[ScheduledPropertyCashflow, ...]


_EMPTY_LANDLORD_RENTAL_WIRING = LandlordRentalWiring(
    initial_cash=(), recurring_property_cashflows=(), scheduled_property_cashflows=()
)


@dataclass(frozen=True)
class _RentalCashflowSegment:
    start_month: int
    end_month: int
    fraction_rented: Decimal


@dataclass(frozen=True)
class _RentalCashflowTerms:
    base_monthly_rent: Decimal
    vacancy_multiplier: Decimal


def _wire_landlord_rental(
    purchase: PropertyPurchase,
    *,
    property_: Property,
    primary_agent_id: AgentId,
    horizon_months: int,
    currency_quantum: Decimal,
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

    initial_cash: list[InitialAccountBalance] = [
        InitialAccountBalance(agent_id=TENANT_AGENT_ID, account_id=TENANT_ACCOUNT_ID, balance=0)
    ]
    recurring_property_cashflows: list[RecurringPropertyCashflow] = []
    scheduled_property_cashflows: list[ScheduledPropertyCashflow] = []
    for segment in rental_segments:
        leased_monthly_rent = base_monthly_rent * segment.fraction_rented
        base_monthly_collected = round_currency_amount(
            leased_monthly_rent * vacancy_multiplier, quantum=currency_quantum
        )
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
                amount=SeriesIndexedAmount(
                    base_amount=base_monthly_collected, series=rent_series, adjustment_period_months=12
                ),
                # Rental income is ordinary income (taxed at owner's marginal bracket).
                # §469 passive-loss limitation is not modeled.
                income_category=ORDINARY_INCOME,
            )
        )

    management = purchase.rental_management
    if management is not None:
        initial_cash.append(
            InitialAccountBalance(
                agent_id=PROPERTY_MANAGEMENT_AGENT_ID, account_id=PROPERTY_MANAGEMENT_ACCOUNT_ID, balance=0
            )
        )
        management_fee_fraction = Decimal(str(management.management_fee_pct)) / Decimal(100)
        if management_fee_fraction > 0:
            for segment in rental_segments:
                base_monthly_collected = round_currency_amount(
                    base_monthly_rent * segment.fraction_rented * vacancy_multiplier, quantum=currency_quantum
                )
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
                        amount=SeriesIndexedAmount(
                            base_amount=round_currency_amount(
                                base_monthly_collected * management_fee_fraction, quantum=currency_quantum
                            ),
                            series=rent_series,
                            adjustment_period_months=12,
                        ),
                        # Management fee is a Schedule E deduction against rental income.
                        deduction_category="ordinary",
                    )
                )
        leasing_fee_months_val = Decimal(str(management.leasing_fee_months))
        if leasing_fee_months_val > 0:
            for segment in rental_segments:
                leasing_fee_base = round_currency_amount(
                    base_monthly_rent * segment.fraction_rented * leasing_fee_months_val, quantum=currency_quantum
                )
                scheduled_property_cashflows.extend(
                    ScheduledPropertyCashflow(
                        month=fire_month,
                        property_id=property_.id,
                        cause_id=f"{LEASING_FEE_CAUSE_ID}:{property_.id}:m{fire_month}",
                        from_agent_id=primary_agent_id,
                        from_account_id=PRIMARY_ACCOUNT_ID,
                        to_agent_id=PROPERTY_MANAGEMENT_AGENT_ID,
                        to_account_id=PROPERTY_MANAGEMENT_ACCOUNT_ID,
                        amount=SeriesIndexedAmount(
                            base_amount=leasing_fee_base, series=rent_series, adjustment_period_months=12
                        ),
                        # Leasing fee is a Schedule E deduction against rental income.
                        deduction_category="ordinary",
                    )
                    for fire_month in range(
                        segment.start_month, segment.end_month + 1, int(management.avg_tenancy_months)
                    )
                )
    return LandlordRentalWiring(
        initial_cash=tuple(initial_cash),
        recurring_property_cashflows=tuple(recurring_property_cashflows),
        scheduled_property_cashflows=tuple(scheduled_property_cashflows),
    )


def _rental_cashflow_terms(purchase: PropertyPurchase, *, property_: Property) -> _RentalCashflowTerms | None:
    if purchase.initial_rental is not None:
        return _RentalCashflowTerms(
            base_monthly_rent=_resolve_monthly_rent(purchase.initial_rental, property_=property_),
            vacancy_multiplier=Decimal(1) - Decimal(str(purchase.initial_rental.vacancy_pct)),
        )
    if not _has_positive_rented_fraction_event(purchase):
        return None
    if property_.rent_estimate is None:
        raise ValueError(
            f"property {property_.id!r} has a future rented-fraction event but no rent_estimate; "
            "set initial_rental.full_property_monthly_rent so product lowering knows full-property rent"
        )
    return _RentalCashflowTerms(
        base_monthly_rent=_amount(property_.rent_estimate),
        vacancy_multiplier=Decimal(1) - Decimal(str(RentalIncomePlan().vacancy_pct)),
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
        current_fraction = Decimal(str(event.rented_fraction))
    if current_start <= end_month and current_fraction > 0:
        segments.append(
            _RentalCashflowSegment(start_month=current_start, end_month=end_month, fraction_rented=current_fraction)
        )
    return tuple(segments)


def _down_payment_for(purchase: PropertyPurchase, property_: Property, *, currency_quantum: Decimal) -> Decimal:
    if isinstance(purchase.financing, CashFinancing):
        return _amount(property_.price)
    return round_currency_amount(
        _amount(property_.price) * _amount(purchase.financing.down_payment_pct) / Decimal(100), quantum=currency_quantum
    )


def _sim_mortgage_for(
    purchase: PropertyPurchase, property_: Property, *, currency_quantum: Decimal
) -> SimMortgageFinancing | None:
    if isinstance(purchase.financing, CashFinancing):
        return None
    assert isinstance(purchase.financing, MortgageFinancing)
    return SimMortgageFinancing(
        liability_id=f"{property_.id}_mortgage",
        lender_agent_id=MORTGAGE_LENDER_AGENT_ID,
        lender_account_id=MORTGAGE_LENDER_ACCOUNT_ID,
        # Derived from the rounded down payment rather than rounded independently from the
        # percentage: `ScheduledPropertyPurchase` requires the two to sum to the price exactly,
        # and two half-quantum roundings of a split can each go up and overshoot it by one.
        principal=_amount(property_.price) - _down_payment_for(purchase, property_, currency_quantum=currency_quantum),
        annual_interest_rate=purchase.financing.annual_rate_pct / 100.0,
        term_months=purchase.financing.term_months,
    )


def _sim_property_purchase(
    purchase: PropertyPurchase,
    property_: Property,
    *,
    primary_agent_id: AgentId,
    mortgage: SimMortgageFinancing | None,
    currency_quantum: Decimal,
) -> ScheduledPropertyPurchase:
    purchase_price = _amount(property_.price)
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
        purchase_price=purchase_price,
        down_payment=_down_payment_for(purchase, property_, currency_quantum=currency_quantum),
        buyer_closing_cost=round_currency_amount(
            purchase_price * _amount(purchase.closing_cost_pct) / Decimal(100), quantum=currency_quantum
        ),
        mortgage=mortgage,
        rented_fraction=rented_fraction,
        # The wire schema has no land-fraction field; this uses the sim default.
    )


def _initial_rented_fraction(purchase: PropertyPurchase) -> Decimal:
    return Decimal(str(purchase.initial_rental.fraction_rented)) if purchase.initial_rental is not None else Decimal(0)


def _monthly_spend_amount(scenario_key: ScenarioKey) -> Decimal | SeriesIndexedAmount:
    if scenario_key.spend_index == SpendIndex.INFLATION:
        return SeriesIndexedAmount(
            base_amount=scenario_key.monthly_spend, series=InflationKey(), adjustment_period_months=1
        )
    if scenario_key.spend_index == SpendIndex.NONE:
        return scenario_key.monthly_spend
    raise ValueError(f"unsupported spend_index: {scenario_key.spend_index!r}")


def _funding_household(
    funding_policy: FundingPolicy,
    *,
    primary_agent_id: AgentId,
    initial_lots: tuple[InitialLot, ...],
    tlh_portfolios: tuple[TlhPortfolioSpec, ...],
    quantum: Decimal,
) -> tuple[Callable[[], CashBandHousehold | ClaimPayer], tuple[Decimal | SeriesIndexedAmount, ...]]:
    """The household the wire's cash band + weights describe, and the band bounds it reads.

    Zero-weight entries are the product UI's explicit "never sell" exclusion, not the
    household's sellable zero-weight sleeve. Drop them before choosing the sleeves. A security
    weight naming nothing held is dropped too, so a saved target can outlive the position it
    mentions. A managed weight names a TLH portfolio, whose account the household then draws on;
    a portfolio the owner does not hold is refused, since a portfolio id is not a symbol a
    later snapshot could hold again.

    No sleeves left means the owner never auto-sells, and an unaffordable obligation is ruin.
    That is the honest reading of an empty target — there is no holding it is willing to give
    up — and it is why the wire has no "derive it for me" sentinel. The app never buys.
    """

    holders = [(lot.account_id, lot.asset.symbol) for lot in initial_lots if isinstance(lot.asset, SecurityKey)]
    held = {symbol for _, symbol in holders}
    managed_by_id = {managed.portfolio_id: managed for managed in tlh_portfolios}
    sleeves: list[Sleeve] = []
    for sleeve in funding_policy.sleeve_weights:
        if isinstance(sleeve, ManagedSleeveWeight):
            if sleeve.portfolio_id not in managed_by_id:
                raise ValueError(f"sleeve weights name unknown TLH portfolio {sleeve.portfolio_id!r}")
            if sleeve.weight > 0:
                sleeves.append(ManagedSleeve(portfolio_id=sleeve.portfolio_id, weight=sleeve.weight))
        elif sleeve.weight > 0 and sleeve.symbol in held:
            sleeves.append(SecuritySleeve(asset_id=AssetId(sleeve.symbol), weight=sleeve.weight))
    if not sleeves:
        return partial(ClaimPayer, primary_agent_id), ()
    # Lot accounts in holding order, which is the order their FIFO sales walk, then the portfolios'.
    targeted = {sleeve.asset_id for sleeve in sleeves if isinstance(sleeve, SecuritySleeve)}
    sources = [account_id for account_id, symbol in holders if AssetId(symbol) in targeted] + [
        managed_by_id[sleeve.portfolio_id].account_id for sleeve in sleeves if isinstance(sleeve, ManagedSleeve)
    ]
    band = tuple(
        _band_bound_amount(amount, index_to_inflation=funding_policy.cash_band_index_to_inflation)
        for amount in (funding_policy.cash_floor, funding_policy.cash_ceiling)
    )
    floor, ceiling = (_household_bound(amount, quantum=quantum) for amount in band)
    household = partial(
        CashBandHousehold,
        primary_agent_id,
        cash_account_id=PRIMARY_ACCOUNT_ID,
        floor=floor,
        ceiling=ceiling,
        sleeves=tuple(sleeves),
        source_account_ids=tuple(dict.fromkeys(sources)),
        reinvest=None,
        cause_id_prefix="product_funding_sale",
    )
    return household, band


def _band_bound_amount(amount: Decimal, *, index_to_inflation: bool) -> Decimal | SeriesIndexedAmount:
    """Translate an exact configured amount + index flag into the sim `AmountSpec`.

    An indexed bound tracks CPI monthly (period=1) so the real-terms band stays constant; a
    nominal bound remains the exact configured amount.
    """

    if not index_to_inflation or amount <= 0:
        return amount
    return SeriesIndexedAmount(base_amount=amount, series=InflationKey(), adjustment_period_months=1)


def _household_bound(amount: Decimal | SeriesIndexedAmount, *, quantum: Decimal) -> BandBound:
    if isinstance(amount, Decimal):
        return int(currency_amount_to_quanta(amount, quantum=quantum))
    return CpiIndexed(
        base_amount=int(currency_amount_to_quanta(amount.base_amount, quantum=quantum)),
        adjustment_period_months=int(amount.adjustment_period_months),
    )


def _build_private_equity_tender_policies(
    *, scenario_key: ScenarioKey, initial_lots: tuple[InitialLot, ...], primary_agent_id: AgentId
) -> list[PrivateEquityTenderPolicy]:
    """Build the sim `PrivateEquityTenderPolicy` list from the wire's pe_tender_policy.

    A single policy targets the primary agent. It is emitted whenever the user holds PE,
    even with a zero floor: the floor only controls voluntary tender/public-market sales,
    while exogenous forced-sale/recovery events still need owner/proceeds routing.
    """

    holds_pe = any(isinstance(lot.asset, PrivateEquityAssetKey) for lot in initial_lots)
    floor = scenario_key.pe_tender_policy.liquid_net_worth_floor
    if not holds_pe:
        return []
    if floor > 0 and scenario_key.pe_tender_policy.index_floor_to_inflation:
        floor_amount: FixedAmount | SeriesIndexedAmount = SeriesIndexedAmount(
            base_amount=floor, series=InflationKey(), adjustment_period_months=1
        )
    else:
        floor_amount = FixedAmount(amount=floor)
    return [
        PrivateEquityTenderPolicy(
            owner_agent_id=primary_agent_id, proceeds_account_id=PRIMARY_ACCOUNT_ID, liquid_net_worth_floor=floor_amount
        )
    ]
