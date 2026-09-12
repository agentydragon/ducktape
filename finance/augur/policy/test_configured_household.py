"""The configured household sizes its purchases from the cash its own batch leaves."""

from dataclasses import dataclass

import pytest_bazel
from more_itertools import one

from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book, SecurityLotState
from finance.augur.sim.capture import FinancialCapture, FinancialOutput
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedLot,
    PreparedObligation,
    PreparedSeries,
    PreparedTlhPortfolio,
    _AllocationPolicy,
    _SleeveTarget,
)
from finance.augur.sim.tlh import TlhAssumptions
from finance.augur.sim.world import World

ALICE = "alice"
CREDITOR = "creditor"
CASH = "cash-account"
HOLDINGS = "holdings-account"
# No modeled harvest: the managed sleeve's value moves only with contributions here.
QUIET = TlhAssumptions(
    peak_annual_yield=0, floor_annual_yield=0, maturity_decay_exponent=1, drawdown_sensitivity=0, short_term_fraction=1
)


@dataclass
class Situation:
    """One household's books, its funding policy and the claims raised on it."""

    prices: dict[str, int]
    policy: _AllocationPolicy
    opening_cash: int = 0
    lots: tuple[PreparedLot, ...] = ()
    portfolios: tuple[PreparedTlhPortfolio, ...] = ()
    claims: tuple[PreparedObligation, ...] = ()
    horizon_months: int = 1


def sleeve(asset_id: str, weight: int) -> _SleeveTarget:
    return _SleeveTarget(asset_id=asset_id, weight=weight, quantity_scale=1)


def policy(*sleeves: _SleeveTarget, ceiling: int, tolerance: int | None) -> _AllocationPolicy:
    return _AllocationPolicy(
        agent_id=ALICE,
        account_id=CASH,
        source_account_ids=(HOLDINGS,),
        sleeves=sleeves,
        cash_floor=0,
        cash_ceiling=ceiling,
        cause_id_prefix="fund",
        allow_purchases=True,
        rebalance_tolerance_ppb=tolerance,
    )


def lot(lot_id: str, asset_id: str, *, units: int, basis: int) -> PreparedLot:
    return PreparedLot(
        lot_id=lot_id,
        agent_id=ALICE,
        account_id=HOLDINGS,
        asset_id=asset_id,
        purchase_month=-24,
        quantity_scale=1,
        units=units,
        basis=basis,
    )


def claim(amount: int) -> PreparedObligation:
    return PreparedObligation(
        month=0,
        obligation_id="upkeep",
        obligation_type="cash_spend",
        from_account=AccountRef(agent_id=ALICE, account_id=CASH),
        to_account=AccountRef(agent_id=CREDITOR, account_id=CASH),
        amount_due=amount,
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def run(case: Situation) -> FinancialOutput:
    snapshots = case.horizon_months + 1
    world = World(
        MarketPath(
            [
                PreparedSeries(series_id=f"security:{asset_id}", snapshots=snapshots, values=(price,) * snapshots)
                for asset_id, price in case.prices.items()
            ],
            0,
            rollout_count=1,
        ),
        horizon_months=case.horizon_months,
    )
    for agent_id, account_id, opening in ((ALICE, CASH, case.opening_cash), (ALICE, HOLDINGS, 0), (CREDITOR, CASH, 0)):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=account_id), opening_balance=opening)
        )
    # Every sleeve gets a pool: the household reads its quotes off the positions it observes.
    for asset_id in case.prices:
        world.declare_pool(
            PreparedHoldingPool(agent_id=ALICE, account_id=HOLDINGS, asset_id=asset_id, quantity_scale=1)
        )
    for holding in case.lots:
        world.hold(holding)
    for spec in case.portfolios:
        world.declare_portfolio(spec)
    for obligation in case.claims:
        world.track(Biller(obligation))
    world.track(ConfiguredHousehold(AgentId(ALICE), (case.policy,)))
    recorder = FinancialCapture(world, capture="forensic")
    world.start()
    while not world.finished:
        world.step()
        recorder.record()
    output = recorder.financial()
    assert output is not None
    assert output.failed_month is None
    return output


def balance(book: Book, agent_id: str, account_id: str = CASH) -> int:
    return one(
        row.balance for row in book.balances if (row.account.agent_id, row.account.account_id) == (agent_id, account_id)
    )


def holding(book: Book, lot_id: str) -> SecurityLotState:
    return one(row for row in book.lots if row.lot_id == lot_id)


def test_a_purchase_is_sized_to_what_the_months_claim_payment_leaves() -> None:
    """A quiet drift month: the trim raises less than the buy side wants, and the claim takes 50 more.

    The sleeves are $500 of `coarse` (5 units at $100) against $102 of `fine` (102 units at $1)
    on equal weights, so the rebalance moves $199 each way. The trim can only give up whole
    $100 units, so it raises $100, while $199 buys 199 units of `fine`. Against $50 of opening
    cash and a $50 claim the account holds $100 when the purchase settles, so the exact order
    is 100 units — and an order sized from the pre-payment cash would be rejected and stop the
    path instead.
    """
    output = run(
        Situation(
            prices={"coarse": 100, "fine": 1},
            policy=policy(sleeve("coarse", 1), sleeve("fine", 1), ceiling=1_000, tolerance=0),
            opening_cash=50,
            lots=(
                lot("opening-coarse", "coarse", units=5, basis=500),
                lot("opening-fine", "fine", units=102, basis=102),
            ),
            claims=(claim(50),),
        )
    )
    closed = output.months[1]
    bought = holding(closed, "fund_buy_p0_s1_0")
    assert (bought.units_remaining, bought.basis_remaining, bought.purchase_month) == (100, 100, 0)
    assert holding(closed, "opening-coarse").units_remaining == 4
    assert (balance(closed, ALICE), balance(closed, CREDITOR)) == (0, 50)
    # Exactness is the point: nothing this batch requested was trimmed or refused.
    assert [disposition.units for disposition in output.dispositions] == [1]


def test_a_projected_purchase_into_a_managed_sleeve_contributes_what_is_left() -> None:
    """The sleeve's holding account carries a TLH portfolio, so the order is an opaque contribution.

    $1,000 of cash against a $400 claim and a zero band invests $600, and the contribution is
    the quoted value of the 60 whole units that buys. No household-visible lot is created.
    """
    output = run(
        Situation(
            prices={"index": 10},
            policy=policy(sleeve("index", 1), ceiling=0, tolerance=None),
            opening_cash=1_000,
            portfolios=(
                PreparedTlhPortfolio(
                    portfolio_id="managed",
                    owner_agent_id=ALICE,
                    account_id=HOLDINGS,
                    asset_id="index",
                    quantity_scale=1,
                    initial_cohorts=(lot("opening-index", "index", units=10, basis=100),),
                    assumptions=QUIET,
                ),
            ),
            claims=(claim(400),),
        )
    )
    closed = output.months[1]
    assert one(closed.tlh_portfolios).value == 700  # The $100 opening mark plus the $600 contribution.
    assert closed.lots == []
    assert (balance(closed, ALICE), balance(closed, CREDITOR)) == (0, 400)


if __name__ == "__main__":
    pytest_bazel.main()
