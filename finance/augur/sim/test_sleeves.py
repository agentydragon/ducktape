"""Exact budgets and scoped proposals, settled by the real action executor."""

import json
from decimal import Decimal
from itertools import product
from typing import Any

import pytest
import pytest_bazel

from finance.augur.model.series import SecurityKey
from finance.augur.rust.simulator import Action, ActionSession, DecisionActions, Finished
from finance.augur.sim import sleeves
from finance.augur.sim.scenario import InitialLot
from finance.augur.sim.testing.case import Case, flat, scenario
from finance.augur.sim.testing.fixtures import checking


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


@pytest.fixture
def input_document() -> dict[str, Any]:
    first = SecurityKey(symbol="test-first")
    second = SecurityKey(symbol="test-second")
    case = Case(
        scenario(
            checking(("test-owner", Decimal("0.07")), ("test-world", Decimal(0))),
            horizon_months=2,
            tax_profiles=[],
            initial_lots=[
                InitialLot(
                    lot_id=lot,
                    agent_id="test-owner",
                    account_id=account,
                    asset=asset,
                    purchase_month_index=month,
                    quantity=quantity,
                    cost_basis_per_unit=Decimal("0.03"),
                )
                for account, asset, lot, month, quantity in (
                    ("portfolio", first, "test-newer", -12, Decimal("0.4")),
                    ("portfolio", first, "test-older", -24, Decimal("0.3")),
                    ("portfolio", second, "test-second", -24, Decimal(1)),
                    ("outside", first, "test-outside", -36, Decimal(1)),
                )
            ],
        ),
        rollout_count=1,
        series={asset: flat(Decimal("0.03"), rollout_count=1, horizon_months=2) for asset in (first, second)},
    )
    document = case.compiled_run.execution_input
    # Same economic holdings, different valid pool quantity grids at the native boundary.
    for pool in document["scenario"]["holding_pools"]:
        pool["quantity_scale"] = 1 if pool["account_id"] == "outside" else 10
    for lot in document["scenario"]["initial_lots"]:
        scale = 1 if lot["account_id"] == "outside" else 10
        lot["units"] = lot["units"] * scale // lot["quantity_scale"]
        lot["quantity_scale"] = scale
    return document


def test_fifo_withdrawal_and_exhaustion_preserve_unselected_books(input_document: dict[str, Any]) -> None:
    session = ActionSession(json.dumps(input_document), "test-owner", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        first = sleeves.withdraw(
            batch[0].observation,
            targets={("portfolio", "test-first"): 1},
            cash_account_id="checking",
            amount=1,
            cause_id="first",
        )
        batch = session.advance([DecisionActions(0, 0, first)])
        assert not isinstance(batch, Finished)
        remaining = sleeves.withdraw(
            batch[0].observation,
            targets={("portfolio", "test-first"): 1},
            cash_account_id="checking",
            amount=100,
            cause_id="exhaust",
        )
        remaining.append(
            Action.consume(0, "unfunded", "spending", ("test-owner", "checking"), ("test-world", "checking"), 100)
        )
        remaining.append(Action.transfer("never", ("test-owner", "checking"), ("test-world", "checking"), 1))
        finished = session.advance([DecisionActions(0, 1, remaining)])
        assert isinstance(finished, Finished)
        [result] = json.loads(finished.rollouts_json)
    finally:
        session.close()
    sales = result["trace"]["financial"]["dispositions"]
    assert [(sale["lot_id"], sale["units"], sale["basis"], sale["proceeds"]) for sale in sales] == [
        ("test-older", 3, 1, 1),
        ("test-newer", 4, 1, 1),
    ]
    assert result["stop"] == {"RejectedAction": {"month": 1, "action_index": 1}}
    assert result["summary"]["cash"][0]["values"] == [7, 8, 9]
    lots = {lot["lot_id"]: lot for lot in result["summary"]["ending_book"]["lots"]}
    assert lots["test-outside"]["units_remaining"] == 1
    assert lots["test-second"]["units_remaining"] == 10


@pytest.mark.parametrize("dust", [False, True])
def test_zero_target_full_exit_reentry_and_reserved_cash(input_document: dict[str, Any], dust: bool) -> None:
    if dust:
        input_document["scenario"]["initial_lots"] = [
            lot for lot in input_document["scenario"]["initial_lots"] if lot["lot_id"] != "test-newer"
        ]
        input_document["scenario"]["initial_lots"][0]["units"] = 1
    session = ActionSession(json.dumps(input_document), "test-owner", [0])
    try:
        batch = session.start()
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
            batch = session.advance([DecisionActions(0, month, actions)])
        assert isinstance(batch, Finished)
        [result] = json.loads(batch.rollouts_json)
    finally:
        session.close()
    assert result["stop"] is None
    assert result["summary"]["cash"][0]["values"] == [7, 7, 7]
    first_sales = [sale for sale in result["trace"]["financial"]["dispositions"] if sale["month"] == 0]
    assert sum(sale["units"] for sale in first_sales) == (1 if dust else 7)
    assert sum(sale["proceeds"] for sale in first_sales) == (0 if dust else 2)
    lots = result["summary"]["ending_book"]["lots"]
    assert all(
        lot["units_remaining"] == lot["basis_remaining"] == 0
        for lot in lots
        if lot["asset_id"] == "security:test-second"
    )
    [reentry] = [lot for lot in lots if lot["purchase_month"] == 1]
    assert (reentry["units_remaining"], reentry["basis_remaining"]) == ((10, 3) if dust else (16, 5))
    assert next(lot for lot in lots if lot["lot_id"] == "test-outside")["units_remaining"] == 1


@pytest.mark.parametrize("unheld", [False, True])
def test_deposit_reserves_cash_and_never_buys_zero_target(input_document: dict[str, Any], unheld: bool) -> None:
    if unheld:
        input_document["scenario"]["initial_lots"] = [
            lot for lot in input_document["scenario"]["initial_lots"] if lot["asset_id"] != "test-second"
        ]
    session = ActionSession(json.dumps(input_document), "test-owner", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        observation = batch[0].observation
        targets = {("portfolio", "test-first"): 0, ("portfolio", "test-second"): 1}
        actions = sleeves.deposit(
            observation, targets=targets, cash_account_id="checking", cash_budget=2, cause_id="deposit"
        )
        batch = session.advance([DecisionActions(0, 0, actions)])
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
        session.close()


def test_selected_pools_keep_their_own_economic_unit_scale(input_document: dict[str, Any]) -> None:
    session = ActionSession(json.dumps(input_document), "test-owner", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        actions = sleeves.withdraw(
            batch[0].observation,
            targets={("portfolio", "test-second"): 1, ("outside", "test-first"): 1},
            cash_account_id="checking",
            amount=3,
            cause_id="mixed-grids",
        )
        batch = session.advance([DecisionActions(0, 0, actions)])
        assert not isinstance(batch, Finished)
        assert batch[0].observation.cash == 12
        # Equal 3-quanta sleeves get budgets 2/1 after the stable residual rule.
        # Ceiling to their own grids sells 7/10 of one unit and 1 indivisible unit,
        # for 2+3 quanta: executable proceeds can exceed the 3-quanta request.
        remaining = {(lot.account_id, lot.asset_id): lot.units for lot in batch[0].observation.public_positions}
        assert remaining[("portfolio", "test-second")] == 3
        assert ("outside", "test-first") not in remaining
        finished = session.advance([DecisionActions(0, 1, [])])
        assert isinstance(finished, Finished)
        [result] = json.loads(finished.rollouts_json)
    finally:
        session.close()
    sales = result["trace"]["financial"]["dispositions"]
    assert [(sale["quantity_scale"], sale["units"], sale["proceeds"]) for sale in sales] == [(10, 7, 2), (1, 1, 3)]


if __name__ == "__main__":
    pytest_bazel.main()
