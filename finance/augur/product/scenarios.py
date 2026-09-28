"""Declare the app's situation from a product `ScenarioKey`: prepared once, composed onto one world per path."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import assert_never

from more_itertools import duplicates_everseen, one

from finance.augur.api.config import Config, LocationConfig
from finance.augur.api.portfolio import PortfolioConfig
from finance.augur.api.wire import ActorRole, Property
from finance.augur.model.asset_key import AssetKey, PrivateEquityAssetKey
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
from finance.augur.product.holdings import (
    Bond,
    Distribution,
    Holdings,
    Lot,
    ManagedPortfolio,
    Pool,
    holding_pools,
    opening_lots,
    prepared_bonds,
    prepared_lots,
    prepared_tlh_portfolio,
)
from finance.augur.product.wire import (
    CapitalImprovementEventWire,
    CashFinancing,
    FundingPolicy,
    ManagedSleeveWeight,
    PropertyPurchase,
    PropertySaleEventWire,
    RentalIncomePlan,
    ScenarioKey,
    SetPrimaryResidenceEventWire,
    SetRentedFractionEventWire,
    SpendIndex,
)
from finance.augur.sim import tax
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import ObligationType
from finance.augur.sim.external_series import ExternalSeriesContext, compile_series, level_series_demand
from finance.augur.sim.fixed_point import rate_to_ppb, round_currency_amount, round_ppb
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, LiabilityId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME, InterestIncome, TransferDeductionCategory, TransferIncomeCategory
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import Amount, IndexedAmount, MarketPath, Series
from finance.augur.sim.money import Currency
from finance.augur.sim.pricing import OccupancyMode, insurance_rate, maintenance_rate
from finance.augur.sim.private_equity import TenderPolicy
from finance.augur.sim.private_equity_series import compile_private_equity_series
from finance.augur.sim.property import (
    CapitalImprovement,
    Housing,
    MortgageFinancing,
    Parcel,
    PrimaryResidence,
    PrimaryResidenceEvent,
    RentedFraction,
    ScheduledPurchase,
    ScheduledSale,
)
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.schedule import Once, Recurring, Schedule
from finance.augur.sim.situs import compile_situs
from finance.augur.sim.tax_authority import MortgageInterestDeduction, TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import FilingStatus, TaxProfile, compile_income_sources, compile_profile
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
# The wire has no land-fraction field. 20% land / 80% building is a common cost-segregation
# rule of thumb absent assessor data; only the building share depreciates.
LAND_VALUE_FRACTION_PPB = 200_000_000
# Acquisition-debt principal caps on the mortgage-interest deduction (§163(h)(3)). Federal
# post-TCJA caps it at $750k; California kept its pre-TCJA $1M, so the two diverge for
# moderately large mortgages.
MORTGAGE_INTEREST_PRINCIPAL_CAPS: Mapping[JurisdictionId, Decimal] = {
    JurisdictionId("federal_us"): Decimal(750_000),
    JurisdictionId("california"): Decimal(1_000_000),
}


def _amount(value: object) -> Decimal:
    """Make an existing exact/configured product amount explicit before sim validation."""

    return value if isinstance(value, Decimal) else Decimal(str(value))


def locations_by_id(locations: tuple[LocationConfig, ...]) -> dict[LocationId, LocationConfig]:
    return {location.location_id: location for location in locations}


def resolve_primary_agent_id(augur_config: Config) -> AgentId:
    return one(agent.actor_id for agent in augur_config.agents if agent.role == ActorRole.PRIMARY_OWNER)


def asset_labels(portfolio: PortfolioConfig) -> dict[AssetKey, str]:
    # TLH portfolios need no entry: their events name the portfolio, never an asset.
    return {
        position.asset: f"{position.label or position.display_symbol} ({position.display_symbol})"
        for position in portfolio.holdings
    }


@dataclass(frozen=True, kw_only=True)
class PropertyCashflow:
    """A cashflow that moves only while its property is held."""

    cause_id: str
    from_account: AccountRef
    to_account: AccountRef
    amount: Amount
    income_category: TransferIncomeCategory | None
    deduction_category: TransferDeductionCategory | None
    property_id: PropertyId
    schedule: Schedule


@dataclass(frozen=True, kw_only=True)
class Obligation:
    """A bill the household owes a counterparty; one attached to a property is billed while it is held."""

    obligation_id: str
    obligation_type: ObligationType
    from_account: AccountRef
    to_account: AccountRef
    amount_due: Amount
    property_id: PropertyId | None
    deduction_category: TransferDeductionCategory | None
    deductible_fraction_ppb: int
    schedule: Schedule


@dataclass(frozen=True)
class Home:
    """The purchased property: its tables, the authority that taxes it and the cashflows it carries."""

    housing: Housing
    property_tax: PropertyTaxPolicy
    # Claimed only on a financed primary residence.
    interest_deduction: MortgageInterestDeduction | None
    cashflows: tuple[PropertyCashflow, ...]


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
    income_sources: tuple[TransferIncomeCategory, ...]
    # Each declared account with its opening balance.
    accounts: tuple[tuple[AccountRef, int], ...]
    tax_profile: tax.TaxProfile
    pools: tuple[Pool, ...]
    lots: tuple[Lot, ...]
    tlh_portfolios: tuple[ManagedPortfolio, ...]
    bonds: tuple[Bond, ...]
    home: Home | None
    distributions: tuple[Distribution, ...]
    tender_policy: TenderPolicy | None
    obligations: tuple[Obligation, ...]


def build_situation(
    scenario_key: ScenarioKey,
    *,
    primary_agent_id: AgentId,
    initial_cash: Decimal,
    holdings: Holdings,
    properties_by_id: dict[PropertyId, Property],
    locations: Mapping[LocationId, LocationConfig],
) -> Situation:
    horizon_months = int(scenario_key.horizon_months)
    end_month = horizon_months - 1
    currency = Currency(code=scenario_key.currency_code, quantum=scenario_key.currency_quantum)
    primary = AccountRef(agent_id=primary_agent_id, account_id=PRIMARY_ACCOUNT_ID)

    accounts = [
        (primary, currency.quanta(initial_cash)),
        _empty_account(SPEND_SINK_AGENT_ID, SPEND_SINK_ACCOUNT_ID),
        _empty_account(TAX_AUTHORITY_AGENT_ID, TAX_AUTHORITY_ACCOUNT_ID),
    ]
    obligations = [
        Obligation(
            schedule=Recurring(start_month=0, end_month=end_month),
            obligation_id=SPEND_OBLIGATION_ID,
            obligation_type=ObligationType.CASH_SPEND,
            from_account=primary,
            to_account=AccountRef(agent_id=SPEND_SINK_AGENT_ID, account_id=SPEND_SINK_ACCOUNT_ID),
            amount_due=_monthly_spend_amount(scenario_key, currency=currency),
            property_id=None,
            deduction_category=None,
            deductible_fraction_ppb=rate_to_ppb(1),
        )
    ]

    if scenario_key.monthly_rent > 0:
        assert scenario_key.rental_location_id is not None  # wire validator guarantees
        accounts.append(_empty_account(LANDLORD_AGENT_ID, LANDLORD_ACCOUNT_ID))
        obligations.append(
            Obligation(
                schedule=Recurring(start_month=0, end_month=end_month),
                obligation_id=RENT_OBLIGATION_ID,
                obligation_type=ObligationType.OUTSIDE_RENT,
                from_account=primary,
                to_account=AccountRef(agent_id=LANDLORD_AGENT_ID, account_id=LANDLORD_ACCOUNT_ID),
                amount_due=_indexed(
                    currency.quanta(scenario_key.monthly_rent),
                    RentKey(location_id=LocationId(scenario_key.rental_location_id)),
                    adjustment_period_months=12,
                ),
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=rate_to_ppb(1),
            )
        )

    profile = TaxProfile(
        agent_id=primary_agent_id,
        filing_status=FilingStatus.SINGLE,
        jurisdiction_ids=[JurisdictionId("federal_us"), JurisdictionId("california")],
        tax_authority_agent_id=TAX_AUTHORITY_AGENT_ID,
        payment_account_id=PRIMARY_ACCOUNT_ID,
        tax_authority_account_id=TAX_AUTHORITY_ACCOUNT_ID,
    )
    jurisdictions = load_jurisdictions_for([profile])
    tax_profile = compile_profile(profile, jurisdictions, currency=currency)

    home = None
    if scenario_key.property_purchase is not None:
        purchase = scenario_key.property_purchase
        property_ = properties_by_id[purchase.property_id]
        accounts.append(_empty_account(PROPERTY_SELLER_AGENT_ID, PROPERTY_SELLER_ACCOUNT_ID))
        mortgage = _mortgage_for(purchase, property_, currency=currency)
        interest_deduction = None
        if mortgage is not None:
            accounts.append(_empty_account(MORTGAGE_LENDER_AGENT_ID, MORTGAGE_LENDER_ACCOUNT_ID))
            if purchase.is_primary_residence:
                interest_deduction = MortgageInterestDeduction(
                    liability_id=mortgage.liability_id,
                    owner_agent_id=primary_agent_id,
                    debt_class="acquisition",
                    per_jurisdiction_principal_cap={
                        jurisdiction_id: currency.quanta(cap)
                        for jurisdiction_id, cap in MORTGAGE_INTEREST_PRINCIPAL_CAPS.items()
                    },
                )
        expense_wiring = _wire_property_expenses(
            scenario_key,
            property_=property_,
            primary_agent_id=primary_agent_id,
            horizon_months=horizon_months,
            currency=currency,
        )
        accounts.extend(expense_wiring.accounts)
        obligations.extend(expense_wiring.obligations)
        rental_wiring = _wire_landlord_rental(
            purchase,
            property_=property_,
            primary_agent_id=primary_agent_id,
            horizon_months=horizon_months,
            currency=currency,
        )
        accounts.extend(rental_wiring.accounts)
        home = Home(
            housing=_housing(
                purchase,
                property_,
                primary_agent_id=primary_agent_id,
                parcel=_parcel(property_, locations, currency=currency),
                mortgage=mortgage,
                currency=currency,
            ),
            property_tax=PropertyTaxPolicy(
                property_id=property_.id,
                owner_agent_id=primary_agent_id,
                from_account_id=PRIMARY_ACCOUNT_ID,
                tax_authority_agent_id=TAX_AUTHORITY_AGENT_ID,
                tax_authority_account_id=TAX_AUTHORITY_ACCOUNT_ID,
                # The app's month 0 is January of the year its bundled income-tax tables are law for.
                start_year=one({rules.law_year for rules in tax_profile.jurisdictions}),
                start_month=0,
                end_month=end_month,
            ),
            interest_deduction=interest_deduction,
            cashflows=(*rental_wiring.scheduled_property_cashflows, *rental_wiring.recurring_property_cashflows),
        )

    tender_policy = _tender_policy(
        scenario_key, holdings.portfolio, primary_agent_id=primary_agent_id, currency=currency
    )
    household, band = _funding_household(
        scenario_key.funding_policy, primary_agent_id=primary_agent_id, holdings=holdings, currency=currency
    )
    # The funding policy sells a pool's lots oldest first, so a pool may not hold two lots bought the same month.
    bought = [
        (owner, position.account_id, position.asset.wire_id, -lot.holding_period_months_at_start)
        for owner, position, lot in opening_lots(holdings.portfolio)
    ]
    if len(set(bought)) != len(bought):
        raise ValueError(
            f"duplicate initial lot purchase months for FIFO pool(s): {sorted(set(duplicates_everseen(bought)))}"
        )
    lots = prepared_lots(holdings.portfolio, currency=currency)
    tlh_portfolios = tuple(
        prepared_tlh_portfolio(portfolio, currency=currency) for portfolio in holdings.tlh_portfolios
    )
    bonds = prepared_bonds(holdings.portfolio, coupon_account_id=PRIMARY_ACCOUNT_ID, currency=currency)
    cashflows = () if home is None else home.cashflows
    return Situation(
        currency=currency,
        horizon_months=horizon_months,
        household=household,
        level_series=level_series_demand(
            held_assets=(*(lot.asset_id for lot in lots), *(portfolio.asset_id for portfolio in tlh_portfolios)),
            bond_coupons=(bond.coupon for bond in bonds),
            distributing_assets=(distribution.asset_id for distribution in holdings.distributions),
            amounts=(
                *(cashflow.amount for cashflow in cashflows),
                *(obligation.amount_due for obligation in obligations),
                # Both band bounds, not just the floor: the ceiling is the refill target a raise is
                # sized to, so an indexed ceiling needs its series sampled.
                *band,
            ),
            tender_policies=() if tender_policy is None else (tender_policy,),
            purchases=() if home is None else home.housing.purchases,
        ),
        private_equity_issuers=frozenset(
            position.asset.issuer_id
            for position in holdings.portfolio.holdings
            if isinstance(position.asset, PrivateEquityAssetKey)
        ),
        income_sources=compile_income_sources(
            (
                *(cashflow.income_category for cashflow in cashflows if cashflow.income_category is not None),
                *(InterestIncome(character=bond.character) for bond in bonds),
                *(category for distribution in holdings.distributions for category in distribution.tax_character),
            )
        ),
        accounts=tuple(accounts),
        tax_profile=tax_profile,
        pools=holding_pools(holdings.portfolio),
        lots=lots,
        tlh_portfolios=tlh_portfolios,
        bonds=bonds,
        home=home,
        distributions=holdings.distributions,
        tender_policy=tender_policy,
        obligations=tuple(obligations),
    )


def paths(situation: Situation, sampled: ExternalSeriesContext, *, rollout_count: int) -> tuple[Series, ...]:
    """The sampled paths as the integer series a world reads: levels, then each held issuer's protocol."""
    return (
        *compile_series(
            sampled, rollout_count=rollout_count, horizon_months=situation.horizon_months, currency=situation.currency
        ),
        *compile_private_equity_series(
            sorted(situation.private_equity_issuers),
            sampled.private_equity,
            rollout_count=rollout_count,
            horizon_months=situation.horizon_months,
            currency=situation.currency,
        ),
    )


def compose(situation: Situation, market: MarketPath) -> World:
    """One path's world: the household's books, holdings, home and counterparties, and the household tracked."""
    world = World(market, horizon_months=situation.horizon_months, income_sources=situation.income_sources)
    for account, balance in situation.accounts:
        world.declare_account(account=account, opening_balance=balance)
    world.track(TaxAuthority(situation.tax_profile, indexation=FixedNominalLaw()))
    if situation.home is not None and situation.home.interest_deduction is not None:
        world.declare_deduction(situation.home.interest_deduction)
    for pool in situation.pools:
        world.declare_pool(
            agent_id=pool.agent_id,
            account_id=pool.account_id,
            asset_id=pool.asset_id,
            quantity_scale=pool.quantity_scale,
        )
    for lot in situation.lots:
        world.hold_lot(
            lot_id=lot.lot_id,
            agent_id=lot.agent_id,
            account_id=lot.account_id,
            asset_id=lot.asset_id,
            purchase_month=lot.purchase_month,
            quantity_scale=lot.quantity_scale,
            units=lot.units,
            basis=lot.basis,
        )
    for portfolio in situation.tlh_portfolios:
        world.declare_portfolio(
            portfolio_id=portfolio.portfolio_id,
            owner_agent_id=portfolio.owner_agent_id,
            account_id=portfolio.account_id,
            asset_id=portfolio.asset_id,
            initial_cohorts=portfolio.initial_cohorts,
            assumptions=portfolio.assumptions,
        )
    for bond in situation.bonds:
        world.hold_bond(
            bond_id=bond.bond_id,
            agent_id=bond.agent_id,
            account_id=bond.account_id,
            character=bond.character,
            face_value=bond.face_value,
            purchase_price=bond.purchase_price,
            coupon=bond.coupon,
            coupon_period_months=bond.coupon_period_months,
            purchase_month_index=bond.purchase_month_index,
            maturity_month_index=bond.maturity_month_index,
        )
    if situation.home is not None:
        world.declare_housing(situation.home.housing, (situation.home.property_tax,))
    for distribution in situation.distributions:
        world.declare_distribution(
            agent_id=distribution.agent_id,
            holding_account_id=distribution.holding_account_id,
            asset_id=distribution.asset_id,
            to_account_id=distribution.to_account_id,
            tax_character=distribution.tax_character,
        )
    if situation.tender_policy is not None:
        world.declare_tender_policy(situation.tender_policy)
    if situation.home is not None:
        for flow in situation.home.cashflows:
            world.declare_flow(
                cause_id=flow.cause_id,
                from_account=flow.from_account,
                to_account=flow.to_account,
                amount=flow.amount,
                income_category=flow.income_category,
                deduction_category=flow.deduction_category,
                schedule=flow.schedule,
                property_id=flow.property_id,
            )
    for obligation in situation.obligations:
        world.track(
            Biller(
                obligation_id=obligation.obligation_id,
                obligation_type=obligation.obligation_type,
                from_account=obligation.from_account,
                to_account=obligation.to_account,
                amount_due=obligation.amount_due,
                property_id=obligation.property_id,
                deduction_category=obligation.deduction_category,
                deductible_fraction_ppb=obligation.deductible_fraction_ppb,
                schedule=obligation.schedule,
            )
        )
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

    accounts: tuple[tuple[AccountRef, int], ...]
    obligations: tuple[Obligation, ...]


def _wire_property_expenses(
    scenario_key: ScenarioKey,
    *,
    property_: Property,
    primary_agent_id: AgentId,
    horizon_months: int,
    currency: Currency,
) -> PropertyExpenseWiring:
    """Wire HOA, insurance, and maintenance payees for one purchased property.

    The returned tuple fields are immutable so the caller can merge this property's wiring
    into the scenario's parallel collections without handing mutable lists into the helper.
    Property tax remains a policy rather than a payee obligation and is wired by the caller.
    """

    purchase = scenario_key.property_purchase
    assert purchase is not None
    initial_occupancy_mode, initial_rented_fraction = _initial_occupancy(purchase)
    # When these obligations carry `property_id`, the sim reads the runtime rented fraction at
    # settlement time so mid-horizon stop/restart events resize the Schedule E share.
    deduction_category, deductible_fraction = _schedule_e_split(initial_rented_fraction)

    def bill(
        obligation_id: str, obligation_type: ObligationType, payee: AccountRef, monthly_amount: Decimal
    ) -> Obligation:
        return Obligation(
            schedule=Recurring(start_month=0, end_month=horizon_months - 1),
            obligation_id=obligation_id,
            obligation_type=obligation_type,
            from_account=AccountRef(agent_id=primary_agent_id, account_id=PRIMARY_ACCOUNT_ID),
            to_account=payee,
            amount_due=_indexed(currency.quanta(monthly_amount), InflationKey(), adjustment_period_months=1),
            property_id=property_.id,
            deduction_category=deduction_category,
            # The wire's rented fraction is a float, so the share rounds onto the ppb grid.
            deductible_fraction_ppb=int(round_ppb(deductible_fraction)),
        )

    accounts: list[tuple[AccountRef, int]] = []
    obligations: list[Obligation] = []
    if property_.hoa_monthly > 0:
        accounts.append(_empty_account(HOA_AGENT_ID, HOA_ACCOUNT_ID))
        obligations.append(
            bill(
                HOA_OBLIGATION_ID,
                ObligationType.HOA_DUES,
                AccountRef(agent_id=HOA_AGENT_ID, account_id=HOA_ACCOUNT_ID),
                _amount(property_.hoa_monthly),
            )
        )
    if scenario_key.annual_insurance_pct > 0:
        accounts.append(_empty_account(INSURER_AGENT_ID, INSURER_ACCOUNT_ID))
        effective_insurance_pct = insurance_rate(
            base_annual_pct=float(scenario_key.annual_insurance_pct),
            occupancy_mode=initial_occupancy_mode,
            rented_fraction=initial_rented_fraction,
        )
        obligations.append(
            bill(
                INSURANCE_OBLIGATION_ID,
                ObligationType.HOMEOWNERS_INSURANCE,
                AccountRef(agent_id=INSURER_AGENT_ID, account_id=INSURER_ACCOUNT_ID),
                round_currency_amount(
                    _amount(property_.price) * _amount(effective_insurance_pct) / Decimal(100 * 12),
                    quantum=currency.quantum,
                ),
            )
        )
    if scenario_key.annual_maintenance_pct > 0:
        accounts.append(_empty_account(MAINTENANCE_VENDOR_AGENT_ID, MAINTENANCE_VENDOR_ACCOUNT_ID))
        effective_maintenance_pct = maintenance_rate(
            base_annual_pct=float(scenario_key.annual_maintenance_pct),
            occupancy_mode=initial_occupancy_mode,
            rented_fraction=initial_rented_fraction,
        )
        obligations.append(
            bill(
                MAINTENANCE_OBLIGATION_ID,
                ObligationType.PROPERTY_MAINTENANCE,
                AccountRef(agent_id=MAINTENANCE_VENDOR_AGENT_ID, account_id=MAINTENANCE_VENDOR_ACCOUNT_ID),
                round_currency_amount(
                    _amount(property_.price) * _amount(effective_maintenance_pct) / Decimal(100 * 12),
                    quantum=currency.quantum,
                ),
            )
        )
    return PropertyExpenseWiring(accounts=tuple(accounts), obligations=tuple(obligations))


@dataclass(frozen=True)
class LandlordRentalWiring:
    """Per-property landlord rental wiring produced by `_wire_landlord_rental`. Caller
    extends its parallel account/property cashflow lists with these
    fields — one merge site per property, instead of mutating caller-owned lists
    threaded through the helper as kwargs."""

    accounts: tuple[tuple[AccountRef, int], ...]
    recurring_property_cashflows: tuple[PropertyCashflow, ...]
    scheduled_property_cashflows: tuple[PropertyCashflow, ...]


_EMPTY_LANDLORD_RENTAL_WIRING = LandlordRentalWiring(
    accounts=(), recurring_property_cashflows=(), scheduled_property_cashflows=()
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
    currency: Currency,
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

    owner = AccountRef(agent_id=primary_agent_id, account_id=PRIMARY_ACCOUNT_ID)
    agency = AccountRef(agent_id=PROPERTY_MANAGEMENT_AGENT_ID, account_id=PROPERTY_MANAGEMENT_ACCOUNT_ID)
    accounts = [_empty_account(TENANT_AGENT_ID, TENANT_ACCOUNT_ID)]
    recurring_property_cashflows: list[PropertyCashflow] = []
    scheduled_property_cashflows: list[PropertyCashflow] = []
    for segment in rental_segments:
        leased_monthly_rent = base_monthly_rent * segment.fraction_rented
        base_monthly_collected = round_currency_amount(
            leased_monthly_rent * vacancy_multiplier, quantum=currency.quantum
        )
        recurring_property_cashflows.append(
            PropertyCashflow(
                schedule=Recurring(start_month=segment.start_month, end_month=segment.end_month),
                property_id=property_.id,
                cause_id=f"{RENTAL_INCOME_CAUSE_ID}:{property_.id}",
                from_account=AccountRef(agent_id=TENANT_AGENT_ID, account_id=TENANT_ACCOUNT_ID),
                to_account=owner,
                amount=_indexed(currency.quanta(base_monthly_collected), rent_series, adjustment_period_months=12),
                # Rental income is ordinary income (taxed at owner's marginal bracket).
                # §469 passive-loss limitation is not modeled.
                income_category=ORDINARY_INCOME,
                deduction_category=None,
            )
        )

    management = purchase.rental_management
    if management is not None:
        accounts.append(_empty_account(PROPERTY_MANAGEMENT_AGENT_ID, PROPERTY_MANAGEMENT_ACCOUNT_ID))
        management_fee_fraction = Decimal(str(management.management_fee_pct)) / Decimal(100)
        if management_fee_fraction > 0:
            for segment in rental_segments:
                base_monthly_collected = round_currency_amount(
                    base_monthly_rent * segment.fraction_rented * vacancy_multiplier, quantum=currency.quantum
                )
                recurring_property_cashflows.append(
                    PropertyCashflow(
                        schedule=Recurring(start_month=segment.start_month, end_month=segment.end_month),
                        property_id=property_.id,
                        cause_id=f"{MANAGEMENT_FEE_CAUSE_ID}:{property_.id}",
                        from_account=owner,
                        to_account=agency,
                        amount=_indexed(
                            currency.quanta(
                                round_currency_amount(
                                    base_monthly_collected * management_fee_fraction, quantum=currency.quantum
                                )
                            ),
                            rent_series,
                            adjustment_period_months=12,
                        ),
                        income_category=None,
                        # Management fee is a Schedule E deduction against rental income.
                        deduction_category="ordinary",
                    )
                )
        leasing_fee_months_val = Decimal(str(management.leasing_fee_months))
        if leasing_fee_months_val > 0:
            for segment in rental_segments:
                leasing_fee_base = round_currency_amount(
                    base_monthly_rent * segment.fraction_rented * leasing_fee_months_val, quantum=currency.quantum
                )
                scheduled_property_cashflows.extend(
                    PropertyCashflow(
                        schedule=Once(month=fire_month),
                        property_id=property_.id,
                        cause_id=f"{LEASING_FEE_CAUSE_ID}:{property_.id}:m{fire_month}",
                        from_account=owner,
                        to_account=agency,
                        amount=_indexed(currency.quanta(leasing_fee_base), rent_series, adjustment_period_months=12),
                        income_category=None,
                        # Leasing fee is a Schedule E deduction against rental income.
                        deduction_category="ordinary",
                    )
                    for fire_month in range(
                        segment.start_month, segment.end_month + 1, int(management.avg_tenancy_months)
                    )
                )
    return LandlordRentalWiring(
        accounts=tuple(accounts),
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


def _down_payment_for(purchase: PropertyPurchase, property_: Property, *, currency: Currency) -> Decimal:
    if isinstance(purchase.financing, CashFinancing):
        return _amount(property_.price)
    return round_currency_amount(
        _amount(property_.price) * _amount(purchase.financing.down_payment_pct) / Decimal(100), quantum=currency.quantum
    )


def _mortgage_for(purchase: PropertyPurchase, property_: Property, *, currency: Currency) -> MortgageFinancing | None:
    if isinstance(purchase.financing, CashFinancing):
        return None
    return MortgageFinancing(
        liability_id=LiabilityId(f"{property_.id}_mortgage"),
        lender_agent_id=MORTGAGE_LENDER_AGENT_ID,
        lender_account_id=MORTGAGE_LENDER_ACCOUNT_ID,
        # Derived from the rounded down payment rather than rounded independently from the
        # percentage: the down payment and the principal must sum to the price exactly, and two
        # half-quantum roundings of a split can each go up and overshoot it by one.
        principal=currency.quanta(_amount(property_.price) - _down_payment_for(purchase, property_, currency=currency)),
        # The wire's rate is a float percent, so it rounds onto the ppb grid.
        annual_interest_rate_ppb=int(round_ppb(purchase.financing.annual_rate_pct / 100.0)),
        term_months=purchase.financing.term_months,
    )


def _housing(
    purchase: PropertyPurchase,
    property_: Property,
    *,
    primary_agent_id: AgentId,
    parcel: Parcel,
    mortgage: MortgageFinancing | None,
    currency: Currency,
) -> Housing:
    """The purchase at month 0, its residence assignment and its lifecycle, as the tables `Properties` reads.

    The wire's fractions and percents are floats, so they round onto the ppb grid.
    """

    sales: list[ScheduledSale] = []
    residence_events: list[PrimaryResidenceEvent] = []
    rented_fraction_events: list[RentedFraction] = []
    capital_improvements: list[CapitalImprovement] = []
    for event in purchase.lifecycle_events:
        month = int(event.month)
        match event:
            case SetRentedFractionEventWire():
                rented_fraction_events.append(
                    RentedFraction(
                        month=month,
                        property_id=property_.id,
                        rented_fraction_ppb=int(round_ppb(float(event.rented_fraction))),
                    )
                )
            case SetPrimaryResidenceEventWire():
                residence_events.append(
                    PrimaryResidenceEvent(
                        month=month,
                        agent_id=primary_agent_id,
                        property_id=property_.id if event.is_primary_residence else None,
                    )
                )
            case CapitalImprovementEventWire():
                capital_improvements.append(
                    CapitalImprovement(
                        month=month,
                        property_id=property_.id,
                        amount=currency.quanta(event.amount),
                        description=event.description,
                    )
                )
            case PropertySaleEventWire():
                sales.append(
                    ScheduledSale(
                        month=month,
                        property_id=property_.id,
                        closing_cost_ppb=_closing_cost_ppb(float(event.closing_cost_pct)),
                    )
                )
            case _:
                assert_never(event)
    purchase_price = _amount(property_.price)
    _, rented_fraction = _initial_occupancy(purchase)
    return Housing(
        purchases=(
            ScheduledPurchase(
                month=0,
                cause_id=_purchase_cause_id(property_),
                property_id=property_.id,
                parcel=parcel,
                market=property_.location_id,
                buyer_agent_id=primary_agent_id,
                buyer_account_id=PRIMARY_ACCOUNT_ID,
                seller_agent_id=PROPERTY_SELLER_AGENT_ID,
                seller_account_id=PROPERTY_SELLER_ACCOUNT_ID,
                purchase_price=currency.quanta(purchase_price),
                down_payment=currency.quanta(_down_payment_for(purchase, property_, currency=currency)),
                buyer_closing_cost=currency.quanta(
                    round_currency_amount(
                        purchase_price * _amount(purchase.closing_cost_pct) / Decimal(100), quantum=currency.quantum
                    )
                ),
                rented_fraction_ppb=int(round_ppb(rented_fraction)),
                land_value_fraction_ppb=LAND_VALUE_FRACTION_PPB,
                mortgage=mortgage,
            ),
        ),
        sales=tuple(sales),
        initial_residences=(
            (PrimaryResidence(agent_id=primary_agent_id, property_id=property_.id),)
            if purchase.is_primary_residence
            else ()
        ),
        residence_events=tuple(residence_events),
        rented_fraction_events=tuple(rented_fraction_events),
        capital_improvements=tuple(capital_improvements),
    )


def _closing_cost_ppb(closing_cost_pct: float) -> int:
    """Seller closing costs on the ppb grid every other rate uses: the wire's float percent, rounded."""

    return int(round_ppb(float(Decimal(str(closing_cost_pct)) / 100)))


def _purchase_cause_id(property_: Property) -> str:
    return f"{property_.id}_purchase"


def _parcel(property_: Property, locations: Mapping[LocationId, LocationConfig], *, currency: Currency) -> Parcel:
    """The parcel the purchase buys: taxed under the law of its location's situs."""

    if property_.location_id not in locations:
        known_location_ids = ", ".join(repr(location_id) for location_id in sorted(locations)) or "<none>"
        raise ValueError(
            f"scheduled property purchase {_purchase_cause_id(property_)!r} references unknown location_id "
            f"{property_.location_id!r}; known location ids: {known_location_ids}"
        )
    return Parcel(situs=compile_situs(load_jurisdiction(locations[property_.location_id].situs), currency=currency))


def _initial_rented_fraction(purchase: PropertyPurchase) -> Decimal:
    return Decimal(str(purchase.initial_rental.fraction_rented)) if purchase.initial_rental is not None else Decimal(0)


def _monthly_spend_amount(scenario_key: ScenarioKey, *, currency: Currency) -> Amount:
    if scenario_key.spend_index == SpendIndex.INFLATION:
        return _indexed(currency.quanta(scenario_key.monthly_spend), InflationKey(), adjustment_period_months=1)
    if scenario_key.spend_index == SpendIndex.NONE:
        return currency.quanta(scenario_key.monthly_spend)
    raise ValueError(f"unsupported spend_index: {scenario_key.spend_index!r}")


def _empty_account(agent_id: AgentId, account_id: AccountId) -> tuple[AccountRef, int]:
    """A counterparty's account, opening empty."""

    return AccountRef(agent_id=agent_id, account_id=account_id), 0


def _indexed(base_amount: int, series: InflationKey | RentKey, *, adjustment_period_months: int) -> IndexedAmount:
    """`base_amount` at month 0, reset to the series' level every `adjustment_period_months`."""

    return IndexedAmount(
        base_amount=base_amount,
        series_id=series.wire_id,
        base_month_index=0,
        adjustment_period_months=adjustment_period_months,
    )


def _funding_household(
    funding_policy: FundingPolicy, *, primary_agent_id: AgentId, holdings: Holdings, currency: Currency
) -> tuple[Callable[[], CashBandHousehold | ClaimPayer], tuple[int | IndexedAmount, ...]]:
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

    holders = [
        (position.account_id, position.asset.symbol)
        for _, position, _ in opening_lots(holdings.portfolio)
        if isinstance(position.asset, SecurityKey)
    ]
    held = {symbol for _, symbol in holders}
    managed_by_id = {managed.portfolio_id: managed for managed in holdings.tlh_portfolios}
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
        _band_bound_amount(amount, index_to_inflation=funding_policy.cash_band_index_to_inflation, currency=currency)
        for amount in (funding_policy.cash_floor, funding_policy.cash_ceiling)
    )
    floor, ceiling = (_household_bound(amount) for amount in band)
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


def _band_bound_amount(amount: Decimal, *, index_to_inflation: bool, currency: Currency) -> int | IndexedAmount:
    """Translate an exact configured amount + index flag into an `Amount`.

    An indexed bound tracks CPI monthly (period=1) so the real-terms band stays constant; a
    nominal bound is the exact configured amount in quanta.
    """

    if not index_to_inflation or amount <= 0:
        return currency.quanta(amount)
    return _indexed(currency.quanta(amount), InflationKey(), adjustment_period_months=1)


def _household_bound(amount: int | IndexedAmount) -> BandBound:
    if isinstance(amount, int):
        return amount
    return CpiIndexed(base_amount=amount.base_amount, adjustment_period_months=amount.adjustment_period_months)


def _tender_policy(
    scenario_key: ScenarioKey, portfolio: PortfolioConfig, *, primary_agent_id: AgentId, currency: Currency
) -> TenderPolicy | None:
    """The wire's pe_tender_policy for the primary agent, whenever the portfolio holds PE.

    Emitted even with a zero floor: the floor only controls voluntary tender/public-market
    sales, while exogenous forced-sale/recovery events still need owner/proceeds routing.
    """

    if not any(isinstance(position.asset, PrivateEquityAssetKey) for position in portfolio.holdings):
        return None
    floor = scenario_key.pe_tender_policy.liquid_net_worth_floor
    return TenderPolicy(
        owner_agent_id=primary_agent_id,
        proceeds_account_id=PRIMARY_ACCOUNT_ID,
        liquid_net_worth_floor=(
            _indexed(currency.quanta(floor), InflationKey(), adjustment_period_months=1)
            if floor > 0 and scenario_key.pe_tender_policy.index_floor_to_inflation
            else currency.quanta(floor)
        ),
    )
