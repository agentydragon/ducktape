"""Property state stays scoped to the property it belongs to.

Property tax, Schedule E depreciation, capex, sale basis, §121 eligibility and mortgage
payoff are all per property, and a scenario holding a primary home and a rental is where a
leak between them shows.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import HomeValueKey, LocationId
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import rate_to_ppb
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, LiabilityId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import USD
from finance.augur.sim.property import (
    CapitalImprovement,
    Housing,
    MortgageFinancing,
    PrimaryResidence,
    RentedFraction,
    ScheduledPurchase,
    ScheduledSale,
)
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.results import Rollout
from finance.augur.sim.schedule import Recurring
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import FilingStatus, TaxProfile, compile_profile
from finance.augur.sim.testing.rollouts import book
from finance.augur.sim.testing.series import level_series
from finance.augur.sim.testing.session import each, finish
from finance.augur.sim.testing.situs import START_YEAR, UNTAXED, flat_parcel
from finance.augur.sim.world import World

QUANTA_PER_UNIT = 100
ALICE, SELLER, LENDER, TENANT, COUNTY, IRS = (
    AgentId("alice"),
    AgentId("property_seller"),
    AgentId("lender"),
    AgentId("tenant"),
    AgentId("county"),
    AgentId("irs"),
)
CHECKING = AccountId("checking")
FEDERAL, CALIFORNIA = JurisdictionId("federal_us"), JurisdictionId("california")

LOCATION_ID = LocationId("loc")
HOME_LOCATION_ID, RENTAL_LOCATION_ID = LocationId("home_loc"), LocationId("rental_loc")

# The lifecycle case, in the units its expectations are derived in.
LIFECYCLE_HORIZON = 36
RENTAL_SALE_MONTH = 24
RENTAL_PURCHASE_PRICE = 300_000
RENTAL_CAPEX = 30_000
RENTAL_MONTHLY_TAX = RENTAL_PURCHASE_PRICE * 0.024 / 12.0
RENTAL_BUILDING_BASIS_BEFORE = RENTAL_PURCHASE_PRICE * 0.80
RENTAL_BUILDING_BASIS_AFTER = RENTAL_BUILDING_BASIS_BEFORE + RENTAL_CAPEX
MONTHLY_DEP_BEFORE = RENTAL_BUILDING_BASIS_BEFORE / 27.5 / 12.0
MONTHLY_DEP_AFTER_HALF_RENTED = RENTAL_BUILDING_BASIS_AFTER * 0.5 / 27.5 / 12.0
MONTHLY_RENT = 2_000


# Each market's parcels are taxed at one flat rate.
PARCELS = {
    LOCATION_ID: UNTAXED,
    HOME_LOCATION_ID: flat_parcel(Decimal("0.012")),
    RENTAL_LOCATION_ID: flat_parcel(Decimal("0.024")),
}


def account(agent_id: AgentId, balance: Decimal | int = 0) -> tuple[AccountRef, int]:
    """An account and its opening balance."""
    return AccountRef(agent_id=agent_id, account_id=CHECKING), USD.quanta(balance)


def financing(
    liability_id: LiabilityId, lender: AgentId, *, principal: int, annual_rate: Decimal | int
) -> MortgageFinancing:
    return MortgageFinancing(
        liability_id=liability_id,
        lender_agent_id=lender,
        lender_account_id=CHECKING,
        principal=USD.quanta(principal),
        annual_interest_rate_ppb=rate_to_ppb(annual_rate),
        term_months=360,
    )


def purchase(
    cause_id: str,
    property_id: PropertyId,
    market: LocationId,
    *,
    month: int = 0,
    seller: AgentId = SELLER,
    price: int,
    down: int,
    closing: int = 0,
    rented_fraction: Decimal | int = 0,
    mortgage: MortgageFinancing | None = None,
) -> ScheduledPurchase:
    return ScheduledPurchase(
        month=month,
        cause_id=cause_id,
        property_id=property_id,
        parcel=PARCELS[market],
        market=market,
        buyer_agent_id=ALICE,
        buyer_account_id=CHECKING,
        seller_agent_id=seller,
        seller_account_id=CHECKING,
        purchase_price=USD.quanta(price),
        down_payment=USD.quanta(down),
        buyer_closing_cost=USD.quanta(closing),
        rented_fraction_ppb=rate_to_ppb(rented_fraction),
        land_value_fraction_ppb=rate_to_ppb(Decimal("0.20")),
        mortgage=mortgage,
    )


def property_tax(property_id: PropertyId, collector: AgentId) -> PropertyTaxPolicy:
    return PropertyTaxPolicy(
        property_id=property_id,
        owner_agent_id=ALICE,
        from_account_id=CHECKING,
        tax_authority_agent_id=collector,
        tax_authority_account_id=CHECKING,
        start_year=START_YEAR,
        start_month=0,
        end_month=None,
    )


@dataclass(frozen=True)
class Rent:
    """The tenant's monthly rent to Alice, in dollars, from month 0 through `end_month`."""

    amount: Decimal | int
    end_month: int


@dataclass(frozen=True)
class Situation:
    """What Alice owns, what happens to it, and the market each property is valued in."""

    horizon_months: int
    accounts: tuple[tuple[AccountRef, int], ...]
    housing: Housing
    tax_policies: tuple[PropertyTaxPolicy, ...] = ()
    jurisdiction_ids: tuple[JurisdictionId, ...] = ()
    rent: Rent | None = None
    home_values: Mapping[str, Sequence[float]] = field(default_factory=dict)


def compose(case: Situation) -> World:
    series = level_series(
        {
            HomeValueKey(location_id=LocationId(location_id)): [levels]
            for location_id, levels in case.home_values.items()
        },
        rollout_count=1,
        horizon_months=case.horizon_months,
    )
    jurisdictions = {id_: load_jurisdiction(id_) for id_ in case.jurisdiction_ids}
    world = World(
        MarketPath(series, 0, rollout_count=1), horizon_months=case.horizon_months, income_sources=(ORDINARY_INCOME,)
    )
    for opened, balance in case.accounts:
        world.declare_account(account=opened, opening_balance=balance)
    if case.jurisdiction_ids:
        world.track(
            TaxAuthority(
                compile_profile(
                    TaxProfile(
                        agent_id=ALICE,
                        filing_status=FilingStatus.SINGLE,
                        jurisdiction_ids=list(case.jurisdiction_ids),
                        tax_authority_agent_id=IRS,
                    ),
                    jurisdictions,
                    currency=USD,
                ),
                indexation=FixedNominalLaw(),
            )
        )
    world.declare_housing(case.housing, case.tax_policies)
    if case.rent is not None:
        world.declare_flow(
            schedule=Recurring(start_month=0, end_month=case.rent.end_month),
            cause_id="rental-income:rental",
            from_account=AccountRef(agent_id=TENANT, account_id=CHECKING),
            to_account=AccountRef(agent_id=ALICE, account_id=CHECKING),
            amount=USD.quanta(case.rent.amount),
            income_category=ORDINARY_INCOME,
            deduction_category=None,
        )
    return world


def run(case: Situation) -> Rollout:
    """Alice pays every due claim in full, in order: her installments and her property taxes."""
    return one(finish(ActionSession({0: compose(case)}, ALICE), each(ClaimPayer(AgentId(ALICE)).decide)).rollouts)


def zero_stake_case() -> Situation:
    """One purchase financed to the hilt and one paid for in cash, a month apart."""
    return Situation(
        horizon_months=3,
        accounts=(account(ALICE, 300_000), account(SELLER), account(LENDER)),
        housing=Housing(
            purchases=(
                # No down payment and no closing cost, so the stake is zero while the purchase
                # itself is fully funded.
                purchase(
                    "buy-zero-stake",
                    PropertyId("zero-stake"),
                    LOCATION_ID,
                    month=1,
                    price=100_000,
                    down=0,
                    mortgage=financing(
                        LiabilityId("zero-stake-mortgage"), LENDER, principal=100_000, annual_rate=Decimal("0.06")
                    ),
                ),
                purchase(
                    "buy-positive-stake",
                    PropertyId("positive-stake"),
                    LOCATION_ID,
                    month=2,
                    price=200_000,
                    down=200_000,
                ),
            )
        ),
    )


def home_and_rental_case() -> Situation:
    """One owner holds a primary home and a rental; only the rental changes and sells."""
    flat_home = [1.0] * (LIFECYCLE_HORIZON + 1)
    rental_values = [1.0] * RENTAL_SALE_MONTH + [1.5] * (LIFECYCLE_HORIZON + 1 - RENTAL_SALE_MONTH)
    return Situation(
        horizon_months=LIFECYCLE_HORIZON,
        accounts=(
            account(ALICE, 1_500_000),
            account(TENANT),
            account(AgentId("seller")),
            account(AgentId("bank")),
            account(COUNTY),
            account(IRS),
        ),
        jurisdiction_ids=(FEDERAL, CALIFORNIA),
        rent=Rent(MONTHLY_RENT, end_month=RENTAL_SALE_MONTH - 1),
        housing=Housing(
            purchases=(
                purchase(
                    "buy-home",
                    PropertyId("home"),
                    HOME_LOCATION_ID,
                    seller=AgentId("seller"),
                    price=500_000,
                    down=100_000,
                    mortgage=financing(
                        LiabilityId("home-mortgage"), AgentId("bank"), principal=400_000, annual_rate=Decimal("0.06")
                    ),
                ),
                purchase(
                    "buy-rental",
                    PropertyId("rental"),
                    RENTAL_LOCATION_ID,
                    seller=AgentId("seller"),
                    price=RENTAL_PURCHASE_PRICE,
                    down=RENTAL_PURCHASE_PRICE,
                    rented_fraction=1,
                ),
            ),
            sales=(
                ScheduledSale(
                    month=RENTAL_SALE_MONTH,
                    property_id=PropertyId("rental"),
                    commission_ppb=rate_to_ppb(Decimal("0.06")),
                    escrow_title_ppb=0,
                ),
            ),
            initial_residences=(PrimaryResidence(agent_id=ALICE, property_id=PropertyId("home")),),
            rented_fraction_events=(
                RentedFraction(
                    month=12, property_id=PropertyId("rental"), rented_fraction_ppb=rate_to_ppb(Decimal("0.5"))
                ),
            ),
            capital_improvements=(
                CapitalImprovement(
                    month=12, property_id=PropertyId("rental"), amount=USD.quanta(RENTAL_CAPEX), description="new roof"
                ),
            ),
        ),
        tax_policies=(property_tax(PropertyId("home"), COUNTY), property_tax(PropertyId("rental"), COUNTY)),
        home_values={HOME_LOCATION_ID: flat_home, RENTAL_LOCATION_ID: rental_values},
    )


@pytest.fixture(scope="module")
def lifecycle() -> Rollout:
    return run(home_and_rental_case())


def tax_transfers(rollout: Rollout, prefix: str) -> pl.DataFrame:
    assert rollout.trace is not None
    return rollout.trace.events.transfers.filter(pl.col("cause_id").str.starts_with(prefix)).sort("month_index")


def test_only_a_purchase_with_a_stake_moves_the_buyer_s_cash() -> None:
    rollout = run(zero_stake_case())
    assert rollout.trace is not None

    assert rollout.trace.events.property_purchases.sort("month_index").select("month_index", "cause_id").to_dicts() == [
        {"month_index": 1, "cause_id": "buy-zero-stake"},
        {"month_index": 2, "cause_id": "buy-positive-stake"},
    ]
    # Only the settlement transfers: the financed purchase also pays its mortgage every
    # month, which is not what emits a buyer-cash transfer.
    buyer_cash = rollout.trace.events.transfers.filter(pl.col("cause_id").str.ends_with("_buyer_cash"))
    assert buyer_cash.select("month_index", "cause_id", "amount_quanta").to_dicts() == [
        {"month_index": 2, "cause_id": "buy-positive-stake_buyer_cash", "amount_quanta": 20_000_000}
    ]


def test_property_tax_is_charged_at_each_property_s_own_rate(lifecycle: Rollout) -> None:
    """Two parcels, two rates, and the rental's stops at its sale while the home's does not."""
    home_tax = tax_transfers(lifecycle, "home_property_tax_m")
    assert home_tax.get_column("month_index").to_list() == list(range(1, LIFECYCLE_HORIZON))
    assert home_tax.get_column("amount_quanta").to_list() == [50_000] * (LIFECYCLE_HORIZON - 1)

    rental_tax = tax_transfers(lifecycle, "rental_property_tax_m")
    assert rental_tax.get_column("month_index").to_list() == list(range(1, RENTAL_SALE_MONTH))
    assert rental_tax.get_column("amount_quanta").to_list() == pytest.approx(
        [RENTAL_MONTHLY_TAX * QUANTA_PER_UNIT] * (RENTAL_SALE_MONTH - 1)
    )


def test_a_lifecycle_event_lands_on_the_property_it_names(lifecycle: Rollout) -> None:
    assert lifecycle.trace is not None
    assert lifecycle.trace.events.set_rented_fraction_events.to_dicts() == [
        {"rollout_id": 0, "month_index": 12, "property_id": "rental", "rented_fraction": 0.5}
    ]
    assert lifecycle.trace.events.capital_improvement_events.to_dicts() == [
        {
            "rollout_id": 0,
            "month_index": 12,
            "property_id": "rental",
            "amount_quanta": 3_000_000,
            "description": "new roof",
        }
    ]


def test_the_rental_sale_carries_its_own_basis_and_not_the_home_s_exclusion(lifecycle: Rollout) -> None:
    """§121 belongs to the primary residence, and alice's is `home`.

    The rental's gain is its own: purchase price plus capex less the depreciation actually
    taken, against proceeds net of closing costs. Recapture comes off the long-term gain.
    """
    assert lifecycle.trace is not None
    cumulative_depreciation = 12 * MONTHLY_DEP_BEFORE + 12 * MONTHLY_DEP_AFTER_HALF_RENTED
    gross_proceeds = RENTAL_PURCHASE_PRICE * 1.5 * 0.94
    realized_gain = gross_proceeds - (RENTAL_PURCHASE_PRICE + RENTAL_CAPEX - cumulative_depreciation)

    assert lifecycle.trace.events.rollout_failures.is_empty()
    sale = lifecycle.trace.events.property_sale_events.to_dicts()[0]
    assert sale["month_index"] == RENTAL_SALE_MONTH
    assert sale["property_id"] == "rental"
    assert sale["gross_proceeds_quanta"] == pytest.approx(gross_proceeds * QUANTA_PER_UNIT, abs=100)
    assert sale["mortgage_payoff_quanta"] == 0
    assert sale["realized_gain_quanta"] == pytest.approx(realized_gain * QUANTA_PER_UNIT, abs=100)
    assert sale["depreciation_recapture_quanta"] == pytest.approx(cumulative_depreciation * QUANTA_PER_UNIT, abs=100)
    assert sale["section_121_exclusion_quanta"] == 0
    assert sale["long_term_capital_gain_quanta"] == pytest.approx(
        (realized_gain - cumulative_depreciation) * QUANTA_PER_UNIT, abs=100
    )


def test_the_sold_property_leaves_and_the_held_one_stays(lifecycle: Rollout) -> None:
    terminal = book(lifecycle, LIFECYCLE_HORIZON)
    assert terminal.properties is not None
    held = [state for state in terminal.properties if state.active]
    assert [state.property_id for state in held] == ["home"]
    assert held[0].adjusted_basis == 50_000_000

    mortgage = one(row for row in terminal.mortgages if row.active and row.liability_id == "home-mortgage")
    assert mortgage.principal > 0


def test_each_year_deducts_only_the_depreciation_and_tax_that_year_earned(lifecycle: Rollout) -> None:
    """The rented share changes mid-horizon, so a deduction read from the wrong year — or
    from the wrong property — shows as a different ordinary income."""
    assert lifecycle.trace is not None
    federal = {
        row["month_index"]: row
        for row in lifecycle.trace.events.tax_breakdowns.filter(pl.col("jurisdiction_id") == FEDERAL).iter_rows(
            named=True
        )
    }
    year_rent = 12 * MONTHLY_RENT
    year_0 = year_rent - 12 * MONTHLY_DEP_BEFORE - 11 * RENTAL_MONTHLY_TAX
    year_1 = year_rent - 12 * MONTHLY_DEP_AFTER_HALF_RENTED - 12 * RENTAL_MONTHLY_TAX * 0.5

    assert federal[11]["ordinary_income_quanta"] == pytest.approx(year_0 * QUANTA_PER_UNIT, abs=5)
    assert federal[23]["ordinary_income_quanta"] == pytest.approx(year_1 * QUANTA_PER_UNIT, abs=5)


if __name__ == "__main__":
    pytest_bazel.main()
