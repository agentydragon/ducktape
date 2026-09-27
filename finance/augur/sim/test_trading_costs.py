"""A declared trading-cost schedule: what a buy and a sale pay, where it lands, and how its absence reads.

Every amount is worked by hand in quanta of $0.01: 2.5 basis points is 250,000 parts per billion.
"""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
import pytest_bazel

from finance.augur.sim.actions import Action, Buy, DecisionActions, LotSale, Sell
from finance.augur.sim.books import AccountRef, Book, CapitalGainState, TradingCostOutcome
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, basis_points_to_ppb
from finance.augur.sim.ids import AccountId, AssetId, LotId
from finance.augur.sim.prepared import PreparedHoldingPool, PreparedLot, PreparedSeries, PreparedTradingCosts
from finance.augur.sim.results import Executed, Finished, InvalidRequest, Rejected, Rollout
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, opening, taxpayer, world_on
from finance.augur.sim.world import Capture, World

FUND = AssetId("test_fund")
UNPRICED = AssetId("test_unpriced")
CHECKING = CASH.account_id
BASIS = AccountRef(agent_id=HOUSEHOLD, account_id=AccountId(f"asset-basis:checking:{FUND}"))
GAIN = AccountRef(agent_id=HOUSEHOLD, account_id=AccountId("income:realized-gain"))
SCALE = 1000
RATE = basis_points_to_ppb("2.5")

# 7.5 units at $123.45 settle at $925.875, rounded to $925.88; 2.5 bp of that is 23.147 cents, charged as 23.
BUY = Buy(
    cause_id="test-buy",
    agent_id=HOUSEHOLD,
    cash_account_id=CHECKING,
    holding_account_id=CHECKING,
    asset_id=FUND,
    lot_id=LotId("test-new"),
    quantity_scale=SCALE,
    units=7_500,
)
# At $145.00 the long-held 4 units gross $580.00 and pay 14.5 cents, an exact tie charged as 15; the
# 7.5 bought units gross $1,087.50 and pay 27.1875 cents, charged as 27.
SELL = Sell(
    cause_id="test-sell",
    agent_id=HOUSEHOLD,
    proceeds_account_id=CHECKING,
    asset_id=FUND,
    lots=(
        LotSale(account_id=CHECKING, lot_id=LotId("test-old"), units=4_000),
        LotSale(account_id=CHECKING, lot_id=LotId("test-new"), units=7_500),
    ),
)
SCRIPT: Mapping[int, Sequence[Action]] = {0: [BUY], 1: [SELL]}


def market(*funds: AssetId, cash: int = 0) -> World:
    """The household's checking account and its taxpayer enrollment, over $123.45 then $145.00 a unit."""
    return world_on(
        tuple(
            PreparedSeries(series_id=f"security:{fund}", snapshots=3, values=(12_345, 14_500, 14_500)) for fund in funds
        ),
        horizon_months=2,
        accounts=opening({CASH: cash}),
        taxpayers=(taxpayer(HOUSEHOLD),),
    )


def pool(fund: AssetId) -> PreparedHoldingPool:
    return PreparedHoldingPool(agent_id=HOUSEHOLD, account_id=CHECKING, asset_id=fund, quantity_scale=SCALE)


def composed(rates_ppb: Mapping[AssetId, int] | None, *, cash: int = 1_000_000) -> World:
    """$10,000 of cash and 4 long-held units bought for $400, trading at `rates_ppb`, or with no schedule."""
    world = market(FUND, cash=cash)
    world.declare_pool(pool(FUND))
    world.hold(
        PreparedLot(
            lot_id=LotId("test-old"),
            agent_id=HOUSEHOLD,
            account_id=CHECKING,
            asset_id=FUND,
            purchase_month=-24,
            quantity_scale=SCALE,
            units=4_000,
            basis=40_000,
        )
    )
    if rates_ppb is not None:
        world.declare_trading_costs(PreparedTradingCosts(rates_ppb=rates_ppb))
    return world


def run(world: World, *, capture: Capture = "forensic") -> Rollout:
    """The buy in month 0 and the sale in month 1, through the batch session."""
    session = ActionSession({0: world}, HOUSEHOLD, capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id, decision.observation.month, [*SCRIPT[decision.observation.month]]
                    )
                    for decision in batch
                ]
            )
    finally:
        session.close()
    [rollout] = batch.rollouts
    assert rollout.stop is None
    return rollout


def balance(book: Book, account: AccountRef) -> int:
    [row] = [row for row in book.balances if row.account == account]
    return row.balance


def test_a_buys_cost_joins_the_lot_basis_and_a_sales_comes_off_each_lots_proceeds() -> None:
    rollout = run(composed({FUND: RATE}))
    assert rollout.trace is not None
    _, bought, sold = rollout.trace.books
    assert balance(bought, CASH) == 1_000_000 - 92_588 - 23
    assert [(lot.lot_id, lot.units_remaining, lot.basis_remaining) for lot in bought.lots] == [
        ("test-old", 4_000, 40_000),
        ("test-new", 7_500, 92_588 + 23),
    ]
    assert balance(bought, BASIS) == 40_000 + 92_611
    dispositions = rollout.trace.events.lot_dispositions
    assert dispositions.select(
        "lot_id", "cost_basis_consumed_quanta", "proceeds_quanta", "realized_gain_quanta"
    ).rows() == [
        ("test-old", 40_000, 58_000 - 15, 57_985 - 40_000),
        ("test-new", 92_611, 108_750 - 27, 108_723 - 92_611),
    ]
    assert sold.capital_gains == [CapitalGainState(agent_id=HOUSEHOLD, short_term_gain=16_112, long_term_gain=17_985)]
    assert balance(sold, CASH) == 907_389 + 57_985 + 108_723 == 1_000_000 - 92_588 + 166_750 - (23 + 42)
    assert (balance(sold, BASIS), balance(sold, GAIN)) == (0, -(17_985 + 16_112))


def test_each_trade_pays_its_cost_from_its_own_cash_in_a_separate_entry() -> None:
    rollout = run(composed({FUND: RATE}))
    assert rollout.trace is not None
    assert [
        (entry.cause_id, [(posting.account, posting.amount) for posting in entry.postings])
        for entry in rollout.trace.journal
        if entry.cause_id.startswith(("test-buy", "test-sell"))
    ] == [
        ("test-buy", [(CASH, -92_588), (BASIS, 92_588)]),
        ("test-buy:trading-cost", [(CASH, -23), (BASIS, 23)]),
        ("test-sell", [(CASH, 166_750), (BASIS, -40_000), (BASIS, -92_611), (GAIN, -(166_750 - 132_611))]),
        ("test-sell:trading-cost", [(CASH, -42), (GAIN, 42)]),
    ]
    assert [receipt.outcome for receipt in rollout.trace.receipts] == [
        Executed(trading_cost=23),
        Executed(trading_cost=42),
    ]


@pytest.mark.parametrize("capture", ["summary", "forensic"])
def test_the_compact_summary_lists_every_trades_cost(capture: Capture) -> None:
    rollout = run(composed({FUND: RATE}), capture=capture)
    assert rollout.summary.trading_costs == [
        TradingCostOutcome(
            month=month,
            action_index=0,
            cause_id=cause_id,
            agent_id=HOUSEHOLD,
            account_id=CHECKING,
            asset_id=FUND,
            gross_value=gross,
            cost=cost,
        )
        for month, cause_id, gross, cost in [(0, "test-buy", 92_588, 23), (1, "test-sell", 58_000 + 108_750, 15 + 27)]
    ]
    assert [receipt.outcome for receipt in rollout.summary.last_receipts] == [Executed(trading_cost=42)]


def snapshot(world: World) -> tuple[object, ...]:
    return (
        dict(world.accounting.ledger.balances),
        deepcopy(world.accounting.tax.years),
        list(world.accounting.journal),
        deepcopy(world.holdings.lots),
    )


def test_a_buy_whose_cash_covers_the_trade_but_not_its_cost_is_rejected_and_changes_nothing() -> None:
    free = composed(None, cash=92_588)
    free.start()
    assert free.execute(HOUSEHOLD, BUY).outcome == Executed()
    world = composed({FUND: RATE}, cash=92_588)
    world.start()
    before = snapshot(world)
    assert world.execute(HOUSEHOLD, BUY).outcome == Rejected(
        reason=InvalidRequest(detail="insufficient purchase cash for 92588 and trading cost 23")
    )
    assert snapshot(world) == before
    assert world.trading_costs is not None
    assert not world.trading_costs.paid


def test_a_declared_zero_rate_settles_exactly_as_a_world_without_trading_costs() -> None:
    free, zero = run(composed(None)), run(composed({FUND: 0}))
    assert free.trace is not None
    assert zero.trace is not None
    assert zero.trace.books == free.trace.books
    assert zero.trace.journal == free.trace.journal
    assert zero.trace.events == free.trace.events
    assert (zero.summary.cash, zero.summary.public_holdings) == (free.summary.cash, free.summary.public_holdings)


def test_no_schedule_reads_as_not_modelled_where_a_declared_zero_reads_as_zero() -> None:
    free_world, zero_world = composed(None), composed({FUND: 0})
    free, zero = run(free_world), run(zero_world)
    assert free.trace is not None
    assert zero.trace is not None
    assert free_world.trading_costs is None
    assert free.summary.trading_costs is None
    assert [receipt.outcome for receipt in free.trace.receipts] == [Executed(), Executed()]
    assert zero_world.trading_costs is not None
    assert zero_world.trading_costs.schedule.rates_ppb == {FUND: 0}
    assert zero.summary.trading_costs is not None
    assert [row.cost for row in zero.summary.trading_costs] == [0, 0]
    assert [receipt.outcome for receipt in zero.trace.receipts] == [Executed(trading_cost=0)] * 2


@pytest.mark.parametrize("rate", [-1, MONEY_FACTOR_SCALE + 1], ids=["negative", "above-the-trade"])
def test_a_rate_lies_between_nothing_and_the_whole_trade(rate: int) -> None:
    composed({FUND: MONEY_FACTOR_SCALE})
    with pytest.raises(ValueError, match="must be 0 to 100% of a trade's gross value"):
        composed({FUND: rate})


def test_a_schedule_prices_every_public_pool_declared_before_or_after_it() -> None:
    before = market(FUND)
    before.declare_pool(pool(FUND))
    with pytest.raises(ValueError, match=r"no rate for \['test_fund'\]; there is no default rate"):
        before.declare_trading_costs(PreparedTradingCosts(rates_ppb={UNPRICED: RATE}))
    assert before.trading_costs is None
    after = market(FUND, UNPRICED)
    after.declare_trading_costs(PreparedTradingCosts(rates_ppb={FUND: RATE}))
    after.declare_pool(pool(FUND))
    with pytest.raises(ValueError, match="no rate for 'test_unpriced'; there is no default rate"):
        after.declare_pool(pool(UNPRICED))


@pytest.mark.parametrize(
    ("basis_points", "ppb"), [("2.5", 250_000), (10_000, MONEY_FACTOR_SCALE), (Decimal("0.00001"), 1)]
)
def test_basis_points_convert_exactly_to_parts_per_billion(basis_points: Any, ppb: int) -> None:
    assert basis_points_to_ppb(basis_points) == ppb


@pytest.mark.parametrize(
    ("basis_points", "error"),
    [
        ("NaN", ValueError),
        (Decimal("Infinity"), ValueError),
        (float("nan"), TypeError),
        (2.5, TypeError),
        ("0.000001", ValueError),
    ],
    ids=["nan", "infinite", "float-nan", "float", "finer-than-a-billionth"],
)
def test_a_rate_that_is_inexact_non_finite_or_finer_than_the_grid_is_refused(
    basis_points: Any, error: type[Exception]
) -> None:
    with pytest.raises(error, match=r"finite|not exact|finer than one part per billion"):
        basis_points_to_ppb(basis_points)


if __name__ == "__main__":
    pytest_bazel.main()
