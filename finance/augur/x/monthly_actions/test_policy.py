"""The authored rule reserves already-due claims before proposing an opening buy."""

import pytest
import pytest_bazel

from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import quantity_scale_for_asset
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedHoldingPool, PreparedObligation, PreparedSeries
from finance.augur.sim.results import Finished, RejectedAction
from finance.augur.sim.scenario import ORDINARY_INCOME, ObligationType
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World
from finance.augur.x.monthly_actions.policy import decide
from finance.augur.x.monthly_actions.run import CREDITOR, HOUSEHOLD, STOCK


@pytest.fixture
def opening(bill_dollars: int) -> World:
    """USD 200 in checking, an empty brokerage pool of a USD 100 stock, and a bill due at month 0."""
    world = World(
        MarketPath(
            (PreparedSeries(series_id=f"security:{STOCK.symbol}", snapshots=2, values=(10_000, 10_000)),),
            0,
            rollout_count=1,
        ),
        horizon_months=1,
        income_sources=(ORDINARY_INCOME,),
    )
    for agent_id, balance in ((HOUSEHOLD, 20_000), (CREDITOR, 0)):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id="checking"), opening_balance=balance)
        )
    world.declare_pool(
        PreparedHoldingPool(
            agent_id=HOUSEHOLD,
            account_id="brokerage",
            asset_id=str(STOCK.symbol),
            quantity_scale=quantity_scale_for_asset(STOCK),
        )
    )
    world.track(
        Biller(
            PreparedObligation(
                month=0,
                obligation_id="opening-bill",
                obligation_type=ObligationType.CASH_SPEND,
                from_account=AccountRef(agent_id=HOUSEHOLD, account_id="checking"),
                to_account=AccountRef(agent_id=CREDITOR, account_id="checking"),
                amount_due=bill_dollars * 100,
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=1_000_000_000,
            )
        )
    )
    return world


@pytest.mark.parametrize(
    ("bill_dollars", "bought_units", "paid", "ending_cash"),
    [(150, 500_000, 15_000, 0), (200, 0, 20_000, 0), (250, 0, 0, 20_000)],
)
def test_opening_investment_reserves_claims_and_does_not_rescue_shortfalls(
    opening: World, bought_units: int, paid: int, ending_cash: int
) -> None:
    session = ActionSession({0: opening}, HOUSEHOLD)
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        finished = session.advance(decide(batch))
        assert isinstance(finished, Finished)
        [result] = finished.rollouts
    finally:
        session.close()
    financial = result.trace
    assert financial is not None
    assert [receipt.action.kind for receipt in financial.receipts] == (
        ["Buy", "PayClaim"] if bought_units else ["PayClaim"]
    )
    assert result.summary.payments[0].receipt.amount_paid == paid
    closing = financial.books[-1]
    assert (
        next(
            row.balance
            for row in closing.balances
            if row.account == AccountRef(agent_id=HOUSEHOLD, account_id="checking")
        )
        == ending_cash
    )
    assert [(lot.units_remaining, lot.basis_remaining) for lot in closing.lots] == (
        [(bought_units, 5_000)] if bought_units else []
    )
    assert result.stop == (None if paid else RejectedAction(month=0, action_index=0))
    assert financial.events.lot_dispositions.is_empty()


if __name__ == "__main__":
    pytest_bazel.main()
