"""Property state stays scoped to the property it belongs to.

Two properties is the smallest shape in which an output indexed by property slot can be
wrong. With one property every flattening of `(snapshot, rollout, property)` coincides, so a
reader that applies a rollout-major mask to a property-major buffer is correct by accident;
with two, each property's values land in the other's cells. That has been a real decoder bug
here, which is why the first case below is deliberately as small as it is.

The rest is the same claim over a whole lifecycle: property tax, Schedule E depreciation,
capex, sale basis, §121 eligibility and mortgage payoff are all per property, and a scenario
holding a primary home and a rental is where a leak between them shows.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import HomeValueKey, LocationId
from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb
from finance.augur.sim.ids import AgentId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedRecurringTransfer,
    _CapitalImprovement,
    _MortgageFinancing,
    _PrimaryResidence,
    _PropertyPurchase,
    _PropertySale,
    _PropertyTax,
    _RentedFraction,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME, FilingStatus, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
QUANTA_PER_UNIT = 100
ALICE, SELLER, LENDER, TENANT, COUNTY, IRS = "alice", "property_seller", "lender", "tenant", "county", "irs"
CHECKING = "checking"
FEDERAL, CALIFORNIA = "federal_us", "california"

LOCATION_ID = "loc"
HOME_LOCATION_ID, RENTAL_LOCATION_ID = "home_loc", "rental_loc"

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


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def location(location_id: str, display_name: str, *, annual_rate: float) -> PreparedLocation:
    return PreparedLocation(
        location_id=location_id,
        display_name=display_name,
        jurisdiction_ids=(),
        annual_property_tax_rate_ppb=rate_to_ppb(annual_rate),
        annual_special_assessment=0,
    )


LOCATIONS = (location(LOCATION_ID, "Loc", annual_rate=0.0),)
MULTI_PROPERTY_LOCATIONS = (
    location(HOME_LOCATION_ID, "Primary Home", annual_rate=0.012),
    location(RENTAL_LOCATION_ID, "Rental", annual_rate=0.024),
)


def account(agent_id: str, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=money(balance))


def financing(liability_id: str, lender: str, *, principal: int, annual_rate: float) -> _MortgageFinancing:
    return _MortgageFinancing(
        liability_id=liability_id,
        lender_agent_id=lender,
        lender_account_id=CHECKING,
        principal=money(principal),
        annual_interest_rate_ppb=rate_to_ppb(annual_rate),
        term_months=360,
    )


def purchase(
    cause_id: str,
    property_id: str,
    location_id: str,
    *,
    month: int = 0,
    seller: str = SELLER,
    price: int,
    down: int,
    closing: int = 0,
    rented_fraction: float = 0.0,
    mortgage: _MortgageFinancing | None = None,
) -> _PropertyPurchase:
    return _PropertyPurchase(
        month=month,
        cause_id=cause_id,
        property_id=property_id,
        location_id=location_id,
        buyer_agent_id=ALICE,
        buyer_account_id=CHECKING,
        seller_agent_id=seller,
        seller_account_id=CHECKING,
        purchase_price=money(price),
        down_payment=money(down),
        buyer_closing_cost=money(closing),
        rented_fraction_ppb=rate_to_ppb(rented_fraction),
        land_value_fraction_ppb=rate_to_ppb(0.20),
        mortgage=mortgage,
    )


def property_tax(property_id: str, collector: str) -> _PropertyTax:
    """No rate of its own, so the authority charges the rate of the location the property sits in."""
    return _PropertyTax(
        property_id=property_id,
        owner_agent_id=ALICE,
        from_account_id=CHECKING,
        tax_authority_agent_id=collector,
        tax_authority_account_id=CHECKING,
        annual_tax_rate_ppb=None,
        start_month=0,
        end_month=None,
    )


@dataclass(frozen=True)
class Situation:
    """What Alice owns, what happens to it, and the market each property is valued in."""

    horizon_months: int
    accounts: tuple[PreparedAccount, ...]
    housing: Housing
    rollout_count: int = 1
    locations: tuple[PreparedLocation, ...] = LOCATIONS
    tax_policies: tuple[_PropertyTax, ...] = ()
    jurisdiction_ids: tuple[str, ...] = ()
    recurring_transfers: tuple[PreparedRecurringTransfer, ...] = ()
    home_values: Mapping[str, Sequence[float]] = field(default_factory=dict)


def compose(case: Situation, rollout_id: int) -> World:
    series = compile_series(
        ExternalSeriesContext.from_level_blocks(
            [
                (
                    HomeValueKey(location_id=LocationId(location_id)),
                    np.asarray([levels] * case.rollout_count, dtype=np.float64),
                )
                for location_id, levels in case.home_values.items()
            ],
            rollout_count=case.rollout_count,
            horizon_months=case.horizon_months,
        ),
        rollout_count=case.rollout_count,
        horizon_months=case.horizon_months,
        currency_quantum=QUANTUM,
    )
    jurisdictions = {id_: load_jurisdiction(id_) for id_ in case.jurisdiction_ids}
    world = World(
        MarketPath(series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=tuple(
            PreparedJurisdiction(jurisdiction_id=id_, level=rules.level) for id_, rules in jurisdictions.items()
        ),
    )
    for opening in case.accounts:
        world.declare_account(opening)
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
                    quantum=QUANTUM,
                )
            )
        )
    world.declare_housing(case.housing, case.tax_policies, case.locations)
    world.recurring_transfers = case.recurring_transfers
    return world


def run(case: Situation) -> list[Rollout]:
    """Alice pays each account's claims all or none: her installments and her property taxes."""
    household = ConfiguredHousehold(AgentId(ALICE), ())
    session = ActionSession({id_: compose(case, id_) for id_ in range(case.rollout_count)}, ALICE)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id, decision.observation.month, household.decide(decision.observation)
                    )
                    for decision in batch
                ]
            )
    finally:
        session.close()
    return batch.rollouts


def books(rollout: Rollout) -> list[Book]:
    assert rollout.trace is not None
    return rollout.trace.books


def book(rollout: Rollout, month: int) -> Book:
    return one(entry for entry in books(rollout) if entry.month == month)


def two_property_case() -> Situation:
    """One financed purchase and one all-cash purchase by the same buyer.

    Four distinct stake values, so a cross-assignment between properties and a swap between
    the two columns are both visible. p1 is financed so that its equity ledger differs from
    its purchase price as well: reading the price where the ledger is meant would pass on p2
    alone. The scenario is fully deterministic, so every value must be identical across all
    rollouts and post-purchase months, which is exactly what the flattening bug breaks.
    """
    return Situation(
        horizon_months=3,
        rollout_count=4,
        accounts=(account(ALICE, 2_000_000), account(SELLER), account(LENDER)),
        housing=Housing(
            purchases=(
                purchase(
                    "buy-p1",
                    "p1",
                    LOCATION_ID,
                    price=1_000_000,
                    down=200_000,
                    closing=30_000,
                    mortgage=financing("p1-mortgage", LENDER, principal=800_000, annual_rate=0.06),
                ),
                purchase("buy-p2", "p2", LOCATION_ID, price=500_000, down=500_000, closing=10_000),
            )
        ),
    )


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
                    "zero-stake",
                    LOCATION_ID,
                    month=1,
                    price=100_000,
                    down=0,
                    mortgage=financing("zero-stake-mortgage", LENDER, principal=100_000, annual_rate=0.06),
                ),
                purchase("buy-positive-stake", "positive-stake", LOCATION_ID, month=2, price=200_000, down=200_000),
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
            account("seller"),
            account("bank"),
            account(COUNTY),
            account(IRS),
        ),
        locations=MULTI_PROPERTY_LOCATIONS,
        jurisdiction_ids=(FEDERAL, CALIFORNIA),
        recurring_transfers=(
            PreparedRecurringTransfer(
                start_month=0,
                end_month=RENTAL_SALE_MONTH - 1,
                cause_id="rental-income:rental",
                from_account=AccountRef(agent_id=TENANT, account_id=CHECKING),
                to_account=AccountRef(agent_id=ALICE, account_id=CHECKING),
                amount=money(MONTHLY_RENT),
                income_category=ORDINARY_INCOME,
                deduction_category=None,
            ),
        ),
        housing=Housing(
            purchases=(
                purchase(
                    "buy-home",
                    "home",
                    HOME_LOCATION_ID,
                    seller="seller",
                    price=500_000,
                    down=100_000,
                    mortgage=financing("home-mortgage", "bank", principal=400_000, annual_rate=0.06),
                ),
                purchase(
                    "buy-rental",
                    "rental",
                    RENTAL_LOCATION_ID,
                    seller="seller",
                    price=RENTAL_PURCHASE_PRICE,
                    down=RENTAL_PURCHASE_PRICE,
                    rented_fraction=1.0,
                ),
            ),
            sales=(_PropertySale(month=RENTAL_SALE_MONTH, property_id="rental", closing_cost_ppb=rate_to_ppb(0.06)),),
            initial_residences=(_PrimaryResidence(agent_id=ALICE, property_id="home"),),
            rented_fraction_events=(
                _RentedFraction(month=12, property_id="rental", rented_fraction_ppb=rate_to_ppb(0.5)),
            ),
            capital_improvements=(
                _CapitalImprovement(month=12, property_id="rental", amount=money(RENTAL_CAPEX), description="new roof"),
            ),
        ),
        tax_policies=(property_tax("home", COUNTY), property_tax("rental", COUNTY)),
        home_values={HOME_LOCATION_ID: flat_home, RENTAL_LOCATION_ID: rental_values},
    )


@pytest.fixture(scope="module")
def lifecycle() -> Rollout:
    return one(run(home_and_rental_case()))


def tax_transfers(rollout: Rollout, prefix: str) -> pl.DataFrame:
    assert rollout.trace is not None
    return rollout.trace.events.transfers.filter(pl.col("cause_id").str.starts_with(prefix)).sort("month_index")


def test_property_stakes_are_not_cross_assigned_across_properties() -> None:
    held = [
        state
        for rollout in run(two_property_case())
        for entry in books(rollout)
        for state in entry.properties
        if state.active
    ]

    # equity_ledger = purchase_price - mortgage_principal; contribution_used = down_payment
    # + closing_cost. All four differ, so no pair can be swapped without changing a value.
    expected = {"p1": (23_000_000, 20_000_000), "p2": (51_000_000, 50_000_000)}
    for property_id, stake in expected.items():
        # Deterministic inputs ⇒ exactly one value per property across rollouts and months.
        observed = {
            (state.contribution_used, state.equity_ledger) for state in held if state.property_id == property_id
        }
        assert observed == {stake}, property_id


def test_only_a_purchase_with_a_stake_moves_the_buyer_s_cash() -> None:
    rollout = one(run(zero_stake_case()))
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
    """Two locations, two rates, and the rental's stops at its sale while the home's does not."""
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
    assert lifecycle.trace.events.capital_improvement_events.select(
        "rollout_id", "month_index", "property_id", "amount_quanta"
    ).to_dicts() == [{"rollout_id": 0, "month_index": 12, "property_id": "rental", "amount_quanta": 3_000_000}]


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
