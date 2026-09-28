"""A purchase creates the property, its financing, and the recurring costs of holding it."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import LocationId
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.fixed_point import currency_amount_to_quanta, rate_to_ppb, round_currency_amount
from finance.augur.sim.ids import AccountId, AgentId, LiabilityId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.property import Housing, MortgageFinancing, Parcel, ScheduledPurchase
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.situs import START_YEAR, flat_parcel
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ALICE, SELLER, BANK = AgentId("alice"), AgentId("seller"), AgentId("bank")
CHECKING = AccountId("checking")


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def usd(quanta: Any) -> float:
    """Currency quanta as dollars; a polars column or row hands the count back untyped."""
    return int(quanta) / 100


def cents(value: float) -> float:
    """A derived amount as the engine holds it: exact cents, half away from zero."""
    return float(round_currency_amount(Decimal(str(value)), quantum=QUANTUM))


def account(agent_id: AgentId, balance: Decimal | int = 0) -> tuple[AccountRef, int]:
    """An account and its opening balance."""
    return AccountRef(agent_id=agent_id, account_id=CHECKING), money(balance)


def purchase(
    cause_id: str,
    property_id: PropertyId,
    parcel: Parcel,
    market: LocationId,
    *,
    price: int,
    down: int,
    closing: int = 0,
    mortgage: MortgageFinancing | None = None,
) -> ScheduledPurchase:
    return ScheduledPurchase(
        month=0,
        cause_id=cause_id,
        property_id=property_id,
        parcel=parcel,
        market=market,
        buyer_agent_id=ALICE,
        buyer_account_id=CHECKING,
        seller_agent_id=SELLER,
        seller_account_id=CHECKING,
        purchase_price=money(price),
        down_payment=money(down),
        buyer_closing_cost=money(closing),
        rented_fraction_ppb=0,
        land_value_fraction_ppb=rate_to_ppb(Decimal("0.20")),
        mortgage=mortgage,
    )


def financing(
    liability_id: LiabilityId, *, principal: int, annual_rate: Decimal | int, term_months: int
) -> MortgageFinancing:
    return MortgageFinancing(
        liability_id=liability_id,
        lender_agent_id=BANK,
        lender_account_id=CHECKING,
        principal=money(principal),
        annual_interest_rate_ppb=rate_to_ppb(annual_rate),
        term_months=term_months,
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
class Situation:
    """What Alice buys, where it sits, and who taxes it."""

    horizon_months: int
    accounts: tuple[tuple[AccountRef, int], ...]
    purchases: tuple[ScheduledPurchase, ...]
    tax_policies: tuple[PropertyTaxPolicy, ...] = ()


def compose(case: Situation) -> World:
    world = World(
        MarketPath((), 0, rollout_count=1), horizon_months=case.horizon_months, income_sources=(ORDINARY_INCOME,)
    )
    for opened, balance in case.accounts:
        world.declare_account(account=opened, opening_balance=balance)
    world.declare_housing(Housing(purchases=case.purchases), case.tax_policies)
    return world


def run(case: Situation) -> Rollout:
    """Alice pays every due claim in full, in order: here the installment and the property tax."""
    household = ClaimPayer(AgentId(ALICE))
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


def cash(rollout: Rollout, agent_id: AgentId, month: int) -> float:
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
            accounts=(account(ALICE, 120_000), account(SELLER), account(BANK), account(AgentId("sf_tax_collector"))),
            purchases=(
                purchase(
                    "alice_buys_sf_home",
                    PropertyId("sf_home"),
                    flat_parcel(Decimal("0.012")),
                    LocationId("san_francisco"),
                    price=500_000,
                    down=100_000,
                    closing=10_000,
                    mortgage=financing(
                        LiabilityId("sf_home_mortgage"), principal=400_000, annual_rate=Decimal("0.06"), term_months=360
                    ),
                ),
            ),
            tax_policies=(property_tax(PropertyId("sf_home"), AgentId("sf_tax_collector")),),
        )
    )
    assert rollout.trace is not None

    final_properties = book(rollout, 2).properties
    assert final_properties is not None
    final_property = one(final_properties)
    assert final_property.market == "san_francisco"
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


if __name__ == "__main__":
    pytest_bazel.main()
