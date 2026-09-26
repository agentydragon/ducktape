"""Exact budgets and scoped proposals, settled by the real action executor."""

from dataclasses import replace
from itertools import product

import pytest
import pytest_bazel

from finance.augur.policy import sleeves
from finance.augur.sim.actions import Consume, DecisionActions, Sell, Transfer
from finance.augur.sim.books import AccountRef
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedHoldingPool, PreparedLot, PreparedSeries
from finance.augur.sim.results import Finished, RejectedAction
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World


@pytest.mark.parametrize(
    ("values", "weights", "amount", "withdrawing", "expected"),
    [
        ([90_000, 10_000], [1, 1], 35_000, True, [35_000, 0]),
        ([90_000, 10_000], [1, 1], 90_000, True, [85_000, 5_000]),
        ([90_000, 10_000], [1, 1], 90_000, False, [5_000, 85_000]),
        ([1, 1, 1, 1], [1, 1, 1, 1], 2, True, [1, 1, 0, 0]),
        ([0, 0, 0, 0], [1, 1, 1, 1], 2, False, [0, 0, 1, 1]),
        ([90, 10, 100], [0, 0, 1], 95, True, [90, 5, 0]),
        ([90, 10, 100], [0, 0, 1], 3, False, [0, 0, 3]),
        ([300, 200], [1, 1], 10_000, True, [300, 200]),
        ([2**53 + 1, 0], [0, 1], 2**53, True, [2**53, 0]),
    ],
)
def test_water_filling_expected_amounts(
    values: list[int], weights: list[int], amount: int, withdrawing: bool, expected: list[int]
) -> None:
    assert sleeves._allocate(values, weights, amount, withdrawing=withdrawing) == expected


def test_small_integer_allocations_conserve_cash_and_capacity() -> None:
    for values in product(range(4), repeat=3):
        for weights in product(range(3), repeat=3):
            if not any(weights):
                continue
            for amount in (0, 1, 2, 7, 10):
                taken = sleeves._allocate(list(values), list(weights), amount, withdrawing=True)
                given = sleeves._allocate(list(values), list(weights), amount, withdrawing=False)
                assert sum(taken) == min(amount, sum(values))
                assert all(0 <= part <= value for part, value in zip(taken, values, strict=True))
                assert sum(given) == amount
                assert all(part >= 0 and (weight > 0 or part == 0) for part, weight in zip(given, weights, strict=True))


OWNER = "test-owner"
FIRST, SECOND = "test-first", "test-second"
# Both securities stay at USD 0.03 for the whole horizon.
PRICES = tuple(
    PreparedSeries(series_id=f"security:{asset}", snapshots=3, values=(3, 3, 3)) for asset in (FIRST, SECOND)
)
# Same economic holdings on different valid grids: tenths in the portfolio, whole units outside.
POOLS = tuple(
    PreparedHoldingPool(agent_id=OWNER, account_id=account, asset_id=asset, quantity_scale=scale)
    for account, asset, scale in (("portfolio", FIRST, 10), ("portfolio", SECOND, 10), ("outside", FIRST, 1))
)


@pytest.fixture
def lots() -> tuple[PreparedLot, ...]:
    """0.4 and 0.3 of FIRST and one SECOND in the portfolio, one FIRST outside, each bought at USD 0.03 a unit."""
    return tuple(
        PreparedLot(
            lot_id=lot_id,
            agent_id=OWNER,
            account_id=account,
            asset_id=asset,
            purchase_month=month,
            quantity_scale=scale,
            units=units,
            basis=basis,
        )
        for account, asset, lot_id, month, scale, units, basis in (
            ("portfolio", FIRST, "test-newer", -12, 10, 4, 1),
            ("portfolio", FIRST, "test-older", -24, 10, 3, 1),
            ("portfolio", SECOND, "test-second", -24, 10, 10, 3),
            ("outside", FIRST, "test-outside", -36, 1, 1, 3),
        )
    )


def session(lots: tuple[PreparedLot, ...]) -> ActionSession:
    """The owner's USD 0.07 and `lots` over two months, spending into the world's account."""
    world = World(MarketPath(PRICES, 0, rollout_count=1), horizon_months=2, income_sources=(ORDINARY_INCOME,))
    for agent_id, balance in ((OWNER, 7), ("test-world", 0)):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id="checking"), opening_balance=balance)
        )
    for pool in POOLS:
        world.declare_pool(pool)
    for lot in lots:
        world.hold(lot)
    return ActionSession({0: world}, OWNER)


def test_fifo_withdrawal_and_exhaustion_preserve_unselected_books(lots: tuple[PreparedLot, ...]) -> None:
    live = session(lots)
    try:
        batch = live.start()
        assert not isinstance(batch, Finished)
        first = sleeves.withdraw(
            batch[0].observation,
            targets={("portfolio", "test-first"): 1},
            cash_account_id="checking",
            amount=1,
            cause_id="first",
        )
        batch = live.advance([DecisionActions(0, 0, first)])
        assert not isinstance(batch, Finished)
        remaining = sleeves.withdraw(
            batch[0].observation,
            targets={("portfolio", "test-first"): 1},
            cash_account_id="checking",
            amount=100,
            cause_id="exhaust",
        )
        remaining.append(
            Consume(
                request_id=0,
                cause_id="unfunded",
                component_id="spending",
                from_account=AccountRef(agent_id="test-owner", account_id="checking"),
                to_account=AccountRef(agent_id="test-world", account_id="checking"),
                amount=100,
            )
        )
        remaining.append(
            Transfer(
                cause_id="never",
                from_account=AccountRef(agent_id="test-owner", account_id="checking"),
                to_account=AccountRef(agent_id="test-world", account_id="checking"),
                amount=1,
            )
        )
        finished = live.advance([DecisionActions(0, 1, remaining)])
        assert isinstance(finished, Finished)
        [result] = finished.rollouts
    finally:
        live.close()
    assert result.trace is not None
    sales = result.trace.events.lot_dispositions
    assert sales.select("lot_id", "units_sold", "cost_basis_consumed_quanta", "proceeds_quanta").rows() == [
        ("test-older", 0.3, 1, 1),
        ("test-newer", 0.4, 1, 1),
    ]
    assert result.stop == RejectedAction(month=1, action_index=1)
    assert result.summary.cash[0].values == [7, 8, 9]
    ending = {lot.lot_id: lot for lot in result.summary.ending_book.lots}
    assert ending["test-outside"].units_remaining == 1
    assert ending["test-second"].units_remaining == 10


def test_grouped_symbol_withdrawal_keeps_account_order_and_each_lots_quantity_grid(
    lots: tuple[PreparedLot, ...],
) -> None:
    live = session(lots)
    try:
        batch = live.start()
        assert not isinstance(batch, Finished)
        actions = sleeves.withdraw_by_symbol(
            batch[0].observation,
            targets={"test-first": 1, "test-second": 1},
            source_account_ids=("portfolio", "outside"),
            cash_account_id="checking",
            amount=2,
            cause_id="grouped",
        )
        # FIRST is 5 quanta across scales 10 and 1; SECOND is 3 quanta. Withdraw the
        # two-quanta overweight entirely from FIRST, using portfolio before outside.
        batch = live.advance([DecisionActions(0, 0, actions)])
        assert not isinstance(batch, Finished)
        finished = live.advance([DecisionActions(0, 1, [])])
        assert isinstance(finished, Finished)
        result = finished.rollouts[0]
        assert result.trace is not None
        sales = result.trace.events.lot_dispositions
        assert sales.select("lot_id", "proceeds_quanta").rows() == [("test-older", 1), ("test-newer", 1)]
        remaining = {lot.lot_id: lot.units_remaining for lot in result.summary.ending_book.lots}
        assert remaining == {"test-newer": 0, "test-older": 0, "test-second": 10, "test-outside": 1}
    finally:
        live.close()


@pytest.mark.parametrize("dust", [False, True])
def test_zero_target_full_exit_reentry_and_reserved_cash(lots: tuple[PreparedLot, ...], dust: bool) -> None:
    if dust:
        opening_lots = tuple(lot for lot in lots if lot.lot_id != "test-newer")
        lots = (replace(opening_lots[0], units=1), *opening_lots[1:])
    live = session(lots)
    try:
        batch = live.start()
        for month in (0, 1):
            assert not isinstance(batch, Finished)
            actions = sleeves.rebalance(
                batch[0].observation,
                targets={("portfolio", "test-first"): month, ("portfolio", "test-second"): 1 - month},
                cash_account_id="checking",
                cash_budget=0,
                tolerance_ppb=1_000_000_000,
                cause_id=f"exit-{month}",
            )
            batch = live.advance([DecisionActions(0, month, actions)])
        assert isinstance(batch, Finished)
        [result] = batch.rollouts
    finally:
        live.close()
    assert result.stop is None
    assert result.summary.cash[0].values == [7, 7, 7]
    assert result.trace is not None
    first_sales = [
        receipt.action for receipt in result.trace.receipts if receipt.month == 0 and isinstance(receipt.action, Sell)
    ]
    assert sum(lot.units for sale in first_sales for lot in sale.lots) == (1 if dust else 7)
    assert result.trace.events.at_month(0).lot_dispositions.get_column("proceeds_quanta").sum() == (0 if dust else 2)
    ending = result.summary.ending_book.lots
    assert all(
        lot.units_remaining == lot.basis_remaining == 0 for lot in ending if lot.asset_id == "security:test-second"
    )
    [reentry] = [lot for lot in ending if lot.purchase_month == 1]
    assert (reentry.units_remaining, reentry.basis_remaining) == ((10, 3) if dust else (16, 5))
    assert next(lot for lot in ending if lot.lot_id == "test-outside").units_remaining == 1


@pytest.mark.parametrize("unheld", [False, True])
def test_deposit_reserves_cash_and_never_buys_zero_target(lots: tuple[PreparedLot, ...], unheld: bool) -> None:
    if unheld:
        lots = tuple(lot for lot in lots if lot.asset_id != SECOND)
    live = session(lots)
    try:
        batch = live.start()
        assert not isinstance(batch, Finished)
        observation = batch[0].observation
        targets = {("portfolio", "test-first"): 0, ("portfolio", "test-second"): 1}
        actions = sleeves.deposit(
            observation, targets=targets, cash_account_id="checking", cash_budget=2, cause_id="deposit"
        )
        batch = live.advance([DecisionActions(0, 0, actions)])
        assert not isinstance(batch, Finished)
        assert batch[0].observation.cash == 5
        purchases = [lot for lot in batch[0].observation.public_positions if lot.purchase_month == 0]
        assert [(lot.asset_id, lot.units, lot.book_basis) for lot in purchases] == [("test-second", 6, 2)]
        for invalid in (
            {},
            {("portfolio", "test-first"): 0},
            {("portfolio", "test-first"): -1},
            {("missing", "test-first"): 1},
        ):
            with pytest.raises(ValueError, match=r"positive target|nonnegative|undeclared"):
                sleeves.withdraw(observation, targets=invalid, cash_account_id="checking", amount=0, cause_id="invalid")
        with pytest.raises(ValueError, match="exceeds"):
            sleeves.deposit(
                observation, targets=targets, cash_account_id="checking", cash_budget=8, cause_id="overspend"
            )
        with pytest.raises(ValueError, match="signed-64-bit"):
            sleeves.withdraw(
                observation, targets=targets, cash_account_id="checking", amount=1 << 63, cause_id="overflow"
            )
        with pytest.raises(TypeError):
            sleeves.withdraw(observation, targets=targets, cash_account_id="checking", amount=True, cause_id="boolean")
    finally:
        live.close()


def test_selected_pools_keep_their_own_economic_unit_scale(lots: tuple[PreparedLot, ...]) -> None:
    live = session(lots)
    try:
        batch = live.start()
        assert not isinstance(batch, Finished)
        actions = sleeves.withdraw(
            batch[0].observation,
            targets={("portfolio", "test-second"): 1, ("outside", "test-first"): 1},
            cash_account_id="checking",
            amount=3,
            cause_id="mixed-grids",
        )
        batch = live.advance([DecisionActions(0, 0, actions)])
        assert not isinstance(batch, Finished)
        assert batch[0].observation.cash == 12
        # Equal 3-quanta sleeves get budgets 2/1 after the stable residual rule.
        # Ceiling to their own grids sells 7/10 of one unit and 1 indivisible unit,
        # for 2+3 quanta: executable proceeds can exceed the 3-quanta request.
        remaining = {(lot.account_id, lot.asset_id): lot.units for lot in batch[0].observation.public_positions}
        assert remaining[("portfolio", "test-second")] == 3
        assert ("outside", "test-first") not in remaining
        finished = live.advance([DecisionActions(0, 1, [])])
        assert isinstance(finished, Finished)
        [result] = finished.rollouts
    finally:
        live.close()
    assert result.trace is not None
    sales = result.trace.events.lot_dispositions
    assert sales.select("units_sold", "proceeds_quanta").rows() == [(0.7, 2), (1.0, 3)]
    assert [
        (lot.quantity_scale, lot.units_remaining)
        for lot in result.summary.ending_book.lots
        if lot.lot_id in {"test-second", "test-outside"}
    ] == [(10, 3), (1, 0)]


if __name__ == "__main__":
    pytest_bazel.main()
