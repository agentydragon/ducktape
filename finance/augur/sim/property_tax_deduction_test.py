"""Ad-valorem tax in the income-tax returns, and the ledger entries its bills post.

A single filer on $200,000 of wages buys a $600,000 home outright in January, lets a quarter of
it and lives in the rest. Its parcel is taxed a flat 1.2% of the price, $600 a month from
February: $6,600 in the year, $1,650 of it on the let quarter and $4,950 the owner's. The parcel
also levies San Francisco's transfer tax, all of which the buyer pays: 3.40 × 1,200 = $4,080. The
home is all land, so nothing depreciates.
"""

from dataclasses import replace
from decimal import Decimal
from typing import Any

import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import LocationId
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef, Posting
from finance.augur.sim.fixed_point import rate_to_ppb
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import USD
from finance.augur.sim.property import Housing, Parcel, PrimaryResidence, ScheduledPurchase
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.schedule import Recurring
from finance.augur.sim.session import ActionSession
from finance.augur.sim.situs import compile_situs
from finance.augur.sim.tax_authority import SaltCap, SaltDeduction, TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import TaxProfile, compile_profile
from finance.augur.sim.testing.situs import START_YEAR, flat_parcel
from finance.augur.sim.world import World

ALICE, PAYROLL, IRS, SELLER, COUNTY = (
    AgentId("test-alice"),
    AgentId("test-payroll"),
    AgentId("test-irs"),
    AgentId("test-seller"),
    AgentId("test-county"),
)
CHECKING = AccountId("checking")
HOME = PropertyId("test-home")
FEDERAL, CALIFORNIA = JurisdictionId("federal_us"), JurisdictionId("california")
FLAT = flat_parcel(Decimal("0.012"))
PARCEL = Parcel(
    situs=replace(
        FLAT.situs,
        transfer_taxes=compile_situs(load_jurisdiction(JurisdictionId("san_francisco")), currency=USD).transfer_taxes,
    ),
    prior_assessed_value=None,
)


def dollars(amount: Decimal | int | str) -> int:
    return USD.quanta(Decimal(amount))


def usd(row: dict[str, Any], field: str) -> Decimal:
    return Decimal(row[field]) / 100


def run(cap: int) -> Rollout:
    world = World(MarketPath((), 0, rollout_count=1), horizon_months=12, income_sources=(ORDINARY_INCOME,))
    for agent, balance in ((ALICE, 700_000), (PAYROLL, 300_000), (IRS, 0), (SELLER, 0), (COUNTY, 0)):
        world.declare_account(account=AccountRef(agent_id=agent, account_id=CHECKING), opening_balance=dollars(balance))
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(agent_id=ALICE, jurisdiction_ids=[FEDERAL, CALIFORNIA], tax_authority_agent_id=IRS),
                {id_: load_jurisdiction(id_) for id_ in (FEDERAL, CALIFORNIA)},
                currency=USD,
            ),
            indexation=FixedNominalLaw(),
        )
    )
    world.declare_deduction(
        SaltDeduction(
            profile_id=ALICE,
            federal_jurisdiction_id=FEDERAL,
            cap_schedule=(SaltCap(effective_year_index=0, cap=dollars(cap)),),
        )
    )
    world.declare_housing(
        Housing(
            purchases=(
                ScheduledPurchase(
                    month=0,
                    cause_id="test-purchase",
                    property_id=HOME,
                    parcel=PARCEL,
                    market=LocationId("test-market"),
                    buyer_agent_id=ALICE,
                    buyer_account_id=CHECKING,
                    seller_agent_id=SELLER,
                    seller_account_id=CHECKING,
                    purchase_price=dollars(600_000),
                    down_payment=dollars(600_000),
                    buyer_closing_cost=0,
                    rented_fraction_ppb=rate_to_ppb(Decimal("0.25")),
                    land_value_fraction_ppb=rate_to_ppb(1),
                    mortgage=None,
                    buyer_transfer_tax_share_ppb=rate_to_ppb(1),
                ),
            ),
            initial_residences=(PrimaryResidence(agent_id=ALICE, property_id=HOME),),
        ),
        (
            PropertyTaxPolicy(
                property_id=HOME,
                owner_agent_id=ALICE,
                from_account_id=CHECKING,
                tax_authority_agent_id=COUNTY,
                tax_authority_account_id=CHECKING,
                start_year=START_YEAR,
                start_month=0,
                end_month=None,
            ),
        ),
    )
    world.declare_flow(
        schedule=Recurring(start_month=0, end_month=11),
        cause_id="wages",
        from_account=AccountRef(agent_id=PAYROLL, account_id=CHECKING),
        to_account=AccountRef(agent_id=ALICE, account_id=CHECKING),
        amount=dollars("16666.67"),
        income_category=ORDINARY_INCOME,
        deduction_category=None,
    )
    household = ClaimPayer(ALICE)
    session = ActionSession({0: world}, ALICE)
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
    rollout = one(batch.rollouts)
    assert rollout.stop is None
    return rollout


def returns(rollout: Rollout) -> dict[str, dict[str, Any]]:
    """Each jurisdiction's return for the year."""
    assert rollout.trace is not None
    return {row["jurisdiction_id"]: row for row in rollout.trace.events.tax_breakdowns.to_dicts()}


@pytest.fixture(scope="module")
def under_cap() -> Rollout:
    return run(cap=40_000)


def test_the_let_share_is_a_rental_expense_in_both_returns(under_cap: Rollout) -> None:
    """Wages 12 × 16,666.67 = $200,000.04, less the let quarter's $1,650 of tax."""
    for row in returns(under_cap).values():
        assert usd(row, "ordinary_income_quanta") == Decimal("198350.04")


def test_the_owner_s_share_is_salt_under_the_cap_and_california_itemizes_it_uncapped(under_cap: Rollout) -> None:
    """Federal SALT is the owner's $4,950 plus California's income tax, under a $40,000 cap; the
    $4,080 transfer tax is in neither. California itemizes the $4,950 alone (its own income tax is
    not deductible there), and its $5,363 standard deduction still wins."""
    federal, california = returns(under_cap)[FEDERAL], returns(under_cap)[CALIFORNIA]

    assert usd(federal, "salt_deduction_quanta") == 4_950 + usd(california, "total_tax_quanta")
    assert usd(california, "itemized_deduction_quanta") == 4_950
    assert usd(california, "standard_deduction_quanta") == 5_363


def test_every_bill_moves_the_owner_s_cash_to_the_county(under_cap: Rollout) -> None:
    """Each bill's entry is one debit of Alice's checking and one credit of the county's, balanced;
    the county holds exactly the eleven bills and the transfer tax."""
    assert under_cap.trace is not None
    alice, county = AccountRef(agent_id=ALICE, account_id=CHECKING), AccountRef(agent_id=COUNTY, account_id=CHECKING)
    paid = under_cap.trace.events.obligation_settlements.filter(
        pl.col("obligation_type").is_in(["property_tax", "transfer_tax"])
    )
    entries = {entry.cause_id: entry.postings for entry in under_cap.trace.journal}

    assert sorted(paid.get_column("amount_paid_quanta").to_list()) == [dollars(600)] * 11 + [dollars(4_080)]
    for cause, amount in paid.select("cause_id", "amount_paid_quanta").rows():
        assert entries[cause] == [Posting(account=alice, amount=-amount), Posting(account=county, amount=amount)]
    assert one(row.balance for row in under_cap.trace.books[-1].balances if row.account == county) == dollars(10_680)


if __name__ == "__main__":
    pytest_bazel.main()
