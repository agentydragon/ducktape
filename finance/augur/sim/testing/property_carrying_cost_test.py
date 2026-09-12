"""A purchase creates the property, its financing, and the recurring costs of holding it."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb, round_currency_amount
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedLocation,
    _MortgageFinancing,
    _PropertyPurchase,
    _PropertyTax,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE, SELLER, BANK = "alice", "seller", "bank"
CHECKING = "checking"

SAN_FRANCISCO = PreparedLocation(
    location_id="san_francisco",
    display_name="San Francisco, CA",
    jurisdiction_ids=("federal_us", "california"),
    annual_property_tax_rate_ppb=rate_to_ppb(0.01180),
    annual_special_assessment=0,
)
# Mare Island (Vallejo) carries flat-USD CFD special assessments on top of the ad-valorem rate.
VALLEJO_MARE_ISLAND = PreparedLocation(
    location_id="vallejo_mare_island",
    display_name="Vallejo, CA — Mare Island",
    jurisdiction_ids=("federal_us", "california"),
    annual_property_tax_rate_ppb=rate_to_ppb(0.0115),
    annual_special_assessment=int(currency_amount_to_quanta(Decimal(2300), quantum=QUANTUM)),
)


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def usd(quanta: Any) -> float:
    """Currency quanta as dollars; a polars column or row hands the count back untyped."""
    return int(quanta) / 100


def cents(value: float) -> float:
    """A derived amount as the engine holds it: exact cents, half away from zero."""
    return float(round_currency_amount(Decimal(str(value)), quantum=QUANTUM))


def account(agent_id: str, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=money(balance))


def purchase(
    cause_id: str,
    property_id: str,
    location_id: str,
    *,
    price: int,
    down: int,
    closing: int = 0,
    mortgage: _MortgageFinancing | None = None,
) -> _PropertyPurchase:
    return _PropertyPurchase(
        month=0,
        cause_id=cause_id,
        property_id=property_id,
        location_id=location_id,
        buyer_agent_id=ALICE,
        buyer_account_id=CHECKING,
        seller_agent_id=SELLER,
        seller_account_id=CHECKING,
        purchase_price=money(price),
        down_payment=money(down),
        buyer_closing_cost=money(closing),
        rented_fraction_ppb=0,
        land_value_fraction_ppb=rate_to_ppb(0.20),
        mortgage=mortgage,
    )


def financing(liability_id: str, *, principal: int, annual_rate: float, term_months: int) -> _MortgageFinancing:
    return _MortgageFinancing(
        liability_id=liability_id,
        lender_agent_id=BANK,
        lender_account_id=CHECKING,
        principal=money(principal),
        annual_interest_rate_ppb=rate_to_ppb(annual_rate),
        term_months=term_months,
    )


def property_tax(property_id: str, collector: str, *, annual_rate: float | None) -> _PropertyTax:
    return _PropertyTax(
        property_id=property_id,
        owner_agent_id=ALICE,
        from_account_id=CHECKING,
        tax_authority_agent_id=collector,
        tax_authority_account_id=CHECKING,
        annual_tax_rate_ppb=None if annual_rate is None else rate_to_ppb(annual_rate),
        start_month=0,
        end_month=None,
    )


@dataclass(frozen=True)
class Situation:
    """What Alice buys, where it sits, and who taxes it."""

    horizon_months: int
    accounts: tuple[PreparedAccount, ...]
    purchases: tuple[_PropertyPurchase, ...]
    tax_policies: tuple[_PropertyTax, ...] = ()
    locations: tuple[PreparedLocation, ...] = (SAN_FRANCISCO,)


def compose(case: Situation) -> World:
    world = World(
        MarketPath((), 0, rollout_count=1), horizon_months=case.horizon_months, income_sources=(ORDINARY_INCOME,)
    )
    for opening in case.accounts:
        world.declare_account(opening)
    world.declare_housing(Housing(purchases=case.purchases), case.tax_policies, case.locations)
    return world


def run(case: Situation) -> Rollout:
    """Alice pays each account's claims all or none: here the installment and the property tax."""
    household = ConfiguredHousehold(AgentId(ALICE), ())
    session = ActionSession({0: compose(case)}, ALICE)
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
    return one(batch.rollouts)


def book(rollout: Rollout, month: int) -> Book:
    assert rollout.trace is not None
    return one(entry for entry in rollout.trace.books if entry.month == month)


def cash(rollout: Rollout, agent_id: str, month: int) -> float:
    account_ = AccountRef(agent_id=agent_id, account_id=CHECKING)
    return usd(one(row.balance for row in book(rollout, month).balances if row.account == account_))


def test_real_estate_purchase_mortgage_and_property_tax_numerics() -> None:
    """First real-estate slice: purchase creates property state,
    owner stake, mortgage liability, and monthly carrying-cost cash
    flows. Month 0 books purchase cash; month 1 books one mortgage
    payment and one property-tax transfer."""
    rollout = run(
        Situation(
            horizon_months=2,
            accounts=(account(ALICE, 120_000), account(SELLER), account(BANK), account("sf_tax_collector")),
            purchases=(
                purchase(
                    "alice_buys_sf_home",
                    "sf_home",
                    "san_francisco",
                    price=500_000,
                    down=100_000,
                    closing=10_000,
                    mortgage=financing("sf_home_mortgage", principal=400_000, annual_rate=0.06, term_months=360),
                ),
            ),
            tax_policies=(property_tax("sf_home", "sf_tax_collector", annual_rate=0.012),),
        )
    )
    assert rollout.trace is not None

    final_property = one(book(rollout, 2).properties)
    assert final_property.location_id == "san_francisco"
    assert final_property.purchase_month == 0
    assert usd(final_property.adjusted_basis) == pytest.approx(510_000.0)

    assert final_property.owner_agent_id == ALICE
    assert usd(final_property.contribution_used) == pytest.approx(110_000.0)
    assert usd(final_property.equity_ledger) == pytest.approx(100_000.0)

    mortgage_payment = cents(400_000.0 * 0.005 / (1.0 - (1.005**-360)))
    final_liability = one(row for row in book(rollout, 2).mortgages if row.active)
    assert usd(final_liability.principal) == pytest.approx(400_000.0 - (mortgage_payment - 2_000.0))
    assert usd(final_liability.interest_paid_ytd) == pytest.approx(2_000.0)

    # Property tax: 500_000 * 0.012 / 12 = 500.0 (basis excludes closing cost).
    assert cash(rollout, ALICE, 2) == pytest.approx(120_000.0 - 110_000.0 - mortgage_payment - 500.0)

    events = rollout.trace.events
    assert events.property_purchases.height == 1
    assert events.mortgage_originations.height == 1
    assert events.mortgage_payments.height == 1
    assert events.transfers.filter(pl.col("cause_id") == "sf_home_property_tax_m1").height == 1


def test_real_estate_purchase_requires_known_location() -> None:
    """The declaration rejects a purchase whose location the world was not given."""
    with pytest.raises(
        ValueError,
        match=(
            "scheduled property purchase 'alice_buys_typo_home' references unknown location_id "
            "'san_francsico'; known location ids: 'san_francisco'"
        ),
    ):
        compose(
            Situation(
                horizon_months=1,
                accounts=(account(ALICE, 600_000), account(SELLER)),
                purchases=(
                    purchase("alice_buys_typo_home", "typo_home", "san_francsico", price=500_000, down=500_000),
                ),
            )
        )


def test_property_tax_falls_back_to_location_rate_when_policy_rate_unset() -> None:
    """With no rate on the policy the authority reads the rate of the location the world declared."""
    rollout = run(
        Situation(
            horizon_months=2,
            accounts=(account(ALICE, 600_000), account(SELLER), account("sf_tax_collector")),
            purchases=(purchase("alice_buys_sf_home", "sf_home", "san_francisco", price=500_000, down=500_000),),
            tax_policies=(property_tax("sf_home", "sf_tax_collector", annual_rate=None),),
        )
    )
    # SF: 500_000 * 0.01180 / 12 = 491.6666..., rounded to cents at the obligation boundary.
    assert cash(rollout, "sf_tax_collector", 2) == pytest.approx(cents(500_000.0 * 0.01180 / 12.0))


def test_property_tax_routes_flat_usd_special_assessment_from_location() -> None:
    """A flat-USD CFD special assessment stacks on the ad-valorem tax: ad-valorem + special_usd / 12."""
    rollout = run(
        Situation(
            horizon_months=2,
            accounts=(account(ALICE, 700_000), account(SELLER), account("vallejo_tax_collector")),
            purchases=(
                purchase(
                    "alice_buys_mare_island_home",
                    "mare_island_home",
                    "vallejo_mare_island",
                    price=500_000,
                    down=500_000,
                ),
            ),
            tax_policies=(property_tax("mare_island_home", "vallejo_tax_collector", annual_rate=None),),
            locations=(VALLEJO_MARE_ISLAND,),
        )
    )
    # Mare Island: 500_000 * 0.0115 / 12 + 2300 / 12 per month, rounded to cents.
    expected = cents(500_000.0 * 0.0115 / 12.0 + 2_300.0 / 12.0)
    assert cash(rollout, "vallejo_tax_collector", 2) == pytest.approx(expected)


if __name__ == "__main__":
    pytest_bazel.main()
