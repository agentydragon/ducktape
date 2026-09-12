"""A stopped rollout retains its actual book and reports no later events or snapshots.

A failure in event month f ends at post-event snapshot f+1, using marks observed at f.
Later supplied exogenous marks are not observations of the stopped rollout.
"""

from decimal import Decimal

import pytest_bazel

from finance.augur.model.asset_key import PrivateEquityAssetKey
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode
from finance.augur.product.household import ConfiguredHousehold
from finance.augur.sim.actions import ClaimId, DecisionActions
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.ids import AgentId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLot,
    PreparedObligation,
    PreparedSeries,
    _TenderPolicy,
)
from finance.augur.sim.results import Finished, Rollout, UnpaidClaims
from finance.augur.sim.scenario import ORDINARY_INCOME, ObligationType, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.testing.issuer_protocol import issuer_protocol
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
CHECKING = "checking"
PRIVATE = "private"
FEDERAL = "federal_us"
ALICE = "alice"
VENDOR = "vendor"
IRS = "irs"

TAX_YEAR_MONTHS = 12
# The year closes at month 11 and is assessed at month 12: freezing in the closing month is
# the boundary, where the rollout reaches the end of the year but never the assessment.
FAIL_MONTH = TAX_YEAR_MONTHS - 1

PE_OWNER = "pe_owner"
ISSUER = "acme"
PE_ASSET_ID = "private_equity:acme"
PE_SCALE = quantity_scale_for_asset(PrivateEquityAssetKey(issuer_id=IssuerId(ISSUER)))
PE_FREEZE_MONTH = 1
PE_MARK_MONTHS = 3


def money(amount: Decimal | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount), quantum=QUANTUM))


def account(agent_id: str, account_id: str = CHECKING, balance: Decimal | int = 0) -> PreparedAccount:
    return PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=account_id), opening_balance=money(balance))


def unfundable(*, month: int, payer: str, amount: Decimal | int) -> PreparedObligation:
    """One required payment larger than everything the payer has."""

    return PreparedObligation(
        month=month,
        obligation_id="unfundable",
        obligation_type=ObligationType.CASH_SPEND,
        from_account=AccountRef(agent_id=payer, account_id=CHECKING),
        to_account=AccountRef(agent_id=VENDOR, account_id=CHECKING),
        amount_due=money(amount),
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def frozen_world(*, horizon_months: int) -> World:
    """A taxed agent whose one obligation is larger than everything they have."""

    jurisdictions = {FEDERAL: load_jurisdiction(FEDERAL)}
    world = World(
        MarketPath((), 0, rollout_count=1),
        horizon_months=horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=FEDERAL, level=jurisdictions[FEDERAL].level),),
    )
    for agent_id in (ALICE, VENDOR, IRS):
        world.declare_account(account(agent_id))
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(agent_id=ALICE, jurisdiction_ids=[FEDERAL], tax_authority_agent_id=IRS),
                jurisdictions,
                quantum=QUANTUM,
            )
        )
    )
    world.track(Biller(unfundable(month=FAIL_MONTH, payer=ALICE, amount=Decimal(1))))
    return world


def mark_updates(*, months: int) -> tuple[PreparedSeries, ...]:
    """An issuer that marks itself up every month after the first.

    The marks are exogenous: they come off the path, not from what the run produced, so
    they are the one event stream a frozen rollout could still report. That is exactly why
    this case is here and the plain one above is not enough.
    """

    return issuer_protocol(
        ISSUER,
        horizon_months=months,
        mark_usd=Decimal(100),
        event_kind=[int(PrivateEquityEventKindCode.NONE)]
        + [int(PrivateEquityEventKindCode.ADMIN_MARK_UPDATE)] * months,
    )


def private_equity_world(*, freeze: bool) -> World:
    """A holding whose issuer marks itself up every month, and an owner who may go broke."""

    world = World(
        MarketPath(mark_updates(months=PE_MARK_MONTHS), 0, rollout_count=1),
        horizon_months=PE_MARK_MONTHS,
        income_sources=(ORDINARY_INCOME,),
    )
    for opening in (account(PE_OWNER, balance=Decimal(100)), account(PE_OWNER, PRIVATE), account(VENDOR)):
        world.declare_account(opening)
    world.declare_pool(
        PreparedHoldingPool(agent_id=PE_OWNER, account_id=PRIVATE, asset_id=PE_ASSET_ID, quantity_scale=PE_SCALE)
    )
    world.hold(
        PreparedLot(
            lot_id="pe-acme",
            agent_id=PE_OWNER,
            account_id=PRIVATE,
            asset_id=PE_ASSET_ID,
            purchase_month=-12,
            quantity_scale=PE_SCALE,
            units=int(quantity_to_quanta(10.0, scale=PE_SCALE)),
            basis=money(100),
        )
    )
    world.declare_tender_policy(
        _TenderPolicy(owner_agent_id=PE_OWNER, proceeds_account_id=CHECKING, liquid_net_worth_floor=0)
    )
    if freeze:
        world.track(Biller(unfundable(month=PE_FREEZE_MONTH, payer=PE_OWNER, amount=Decimal(1_000))))
    return world


def run(world: World, actor: str) -> Rollout:
    """The household pays an account's claims only when its cash covers all of them."""

    household = ConfiguredHousehold(AgentId(actor), ())
    session = ActionSession({0: world}, actor)
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
        [rollout] = batch.rollouts
        return rollout
    finally:
        session.close()


def test_the_rollout_really_does_freeze_where_the_case_says() -> None:
    """The premise: without it, "reports nothing later" could hold for the wrong reason."""

    rollout = run(frozen_world(horizon_months=TAX_YEAR_MONTHS), ALICE)

    assert rollout.stop == UnpaidClaims(month=FAIL_MONTH, claims=[ClaimId(month=FAIL_MONTH, index=0)])
    assert rollout.summary.ending_mark_month == FAIL_MONTH


def test_the_issuer_publishes_every_month_when_the_owner_can_pay() -> None:
    """The anchor: without it, an empty frozen result could mean the marks never existed."""

    rollout = run(private_equity_world(freeze=False), PE_OWNER)

    assert rollout.stop is None
    assert rollout.trace is not None
    # The last kind sits at the snapshot past the horizon, so two of the three land in range.
    assert rollout.trace.events.private_equity_events.get_column("month_index").to_list() == [1, 2]


def test_nothing_is_marked_after_the_month_the_rollout_froze() -> None:
    """The issuer keeps marking; the rollout that would have held the position does not.

    These come off the path rather than the run, so nothing about the freeze reaches them
    on its own — they would be reported for months the rollout was no longer around to see
    unless the read model drops them. The rollout freezes at month 1, so month 2 is the one
    this rules out.
    """

    rollout = run(private_equity_world(freeze=True), PE_OWNER)

    assert rollout.trace is not None
    months = rollout.trace.events.private_equity_events.get_column("month_index").to_list()
    assert [month for month in months if month > PE_FREEZE_MONTH] == []


def test_stopped_book_retains_cash_and_private_positions() -> None:
    rollout = run(private_equity_world(freeze=True), PE_OWNER)

    assert rollout.trace is not None
    assert [book.month for book in rollout.trace.books] == [0, 1, 2]
    stopped = rollout.trace.books[-1]
    assert [
        row.balance
        for row in stopped.balances
        if (row.account.agent_id, row.account.account_id) == (PE_OWNER, CHECKING)
    ] == [10_000]
    assert [lot.units_remaining for lot in stopped.lots] == [10_000_000]
    assert [lot.basis_remaining for lot in stopped.lots] == [10_000]


def test_a_tax_year_the_rollout_did_not_survive_is_not_assessed() -> None:
    """The year closes at month 11 and is assessed at month 12; this rollout froze at 11.

    Not a zero assessment — no assessment. A liability that was never created reports
    nothing, which is what an engine that simply never reaches the month does.
    """

    rollout = run(frozen_world(horizon_months=TAX_YEAR_MONTHS + 1), ALICE)

    assert rollout.trace is not None
    assert not [row for book in rollout.trace.books for row in book.tax_liabilities]


if __name__ == "__main__":
    pytest_bazel.main()
