"""Recovery totals survive unit rounding, FIFO order and prior compulsory sales."""

from collections.abc import Callable
from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.capture import FinancialCapture, FinancialOutput
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import CompiledRun, PreparedHoldingPool, PreparedLot, PreparedSeries, _TenderPolicy
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, prepared_scenario
from finance.augur.sim.validation import validate
from finance.augur.sim.world import World


def recovery_run(total: int, positions: tuple[tuple[int, int], ...], *, earlier_sale: bool = False) -> CompiledRun:
    base = prepared_scenario()
    horizon = 2 if earlier_sale else 1
    scenario = replace(
        base,
        horizon_months=horizon,
        accounts=tuple(replace(account, opening_balance=0) for account in base.accounts),
        holding_pools=tuple(
            PreparedHoldingPool(
                agent_id=HOUSEHOLD,
                account_id=f"holding-{index}",
                asset_id="private_equity:test_issuer",
                quantity_scale=scale,
            )
            for index, (_, scale) in enumerate(positions)
        ),
        initial_lots=tuple(
            reversed(
                [
                    PreparedLot(
                        lot_id=f"recovery-{index}",
                        agent_id=HOUSEHOLD,
                        account_id=f"holding-{index}",
                        asset_id="private_equity:test_issuer",
                        purchase_month=-24 + index,
                        quantity_scale=scale,
                        units=units,
                        basis=3 + index,
                    )
                    for index, (units, scale) in enumerate(positions)
                ]
            )
        ),
        _private_equity_tender_policies=(
            _TenderPolicy(owner_agent_id=HOUSEHOLD, proceeds_account_id="checking", liquid_net_worth_floor=0),
        ),
    )
    channels = {
        "mark": 9,
        "regime": 4,
        "event_kind": 6,
        "sale_opportunity": 0,
        "sale_capacity": 1_000_000_000,
        "eligible": 1_000_000_000,
        "forced_sale": 0,
        "liquidity_blocked": 1,
        "forced_recovery": total,
        "company_valuation": 0,
    }
    run = CompiledRun(
        currency_code="USD",
        currency_quantum="1",
        rollout_count=1,
        scenario=scenario,
        series=tuple(
            PreparedSeries(
                series_id=f"private_equity_{channel}:test_issuer",
                snapshots=horizon + 1,
                values=(
                    (0 if channel == "forced_recovery" else 500_000_000 if channel == "forced_sale" else value),
                    value,
                    value,
                )
                if earlier_sale
                else (value, value),
            )
            for channel, value in channels.items()
        ),
    )
    validate(run)
    return run


def composed(run: CompiledRun) -> World:
    """The fixture's world declared piece by piece: pools, lots and the owner's tender policy."""
    scenario = run.scenario
    world = World(MarketPath.from_run(run, 0), horizon_months=scenario.horizon_months)
    for account in scenario.accounts:
        world.declare_account(account)
    for pool in scenario.holding_pools:
        world.declare_pool(pool)
    for lot in scenario.initial_lots:
        world.hold(lot)
    for policy in scenario._private_equity_tender_policies:
        world.declare_tender_policy(policy)
    return world


BUILDS = pytest.mark.parametrize("build", [lambda run: World.from_run(run, 0), composed], ids=["from_run", "composed"])


def execute(run: CompiledRun, build: Callable[[CompiledRun], World]) -> tuple[World, FinancialOutput]:
    world = build(run)
    assert world.private_equity is not None
    capture = FinancialCapture(world, capture="forensic")
    world.start()
    for _ in range(run.scenario.horizon_months):
        world.close_month()
        capture.record()
        if not world.finished:
            world.open_month()
    financial = capture.financial()
    assert financial is not None
    return world, financial


@pytest.mark.parametrize(
    ("total", "positions", "proceeds"),
    [
        pytest.param(1, ((3, 1),), (1,), id="total_one_for_three_units"),
        pytest.param(2, ((3, 1),), (2,), id="total_two_for_three_units"),
        pytest.param(5, ((1, 1), (2, 1), (3, 1)), (1, 2, 2), id="across_lots_and_accounts"),
        pytest.param(2, ((1, 1), (10, 10), (100, 100)), (1, 1, 0), id="economic_units_across_scales"),
    ],
)
@BUILDS
def test_recovery_total(
    total: int, positions: tuple[tuple[int, int], ...], proceeds: tuple[int, ...], build: Callable[[CompiledRun], World]
) -> None:
    world, financial = execute(recovery_run(total, positions), build)
    assert financial.failed_month is None
    assert tuple(row.proceeds for row in financial.dispositions) == proceeds
    assert sum(row.proceeds for row in financial.dispositions) == total
    assert all(lot.units_remaining == lot.basis_remaining == 0 for lot in financial.months[-1].lots)
    assert world.accounting.ledger.balance(CASH) == total
    for index, (row, (units, scale), amount) in enumerate(
        zip(financial.dispositions, positions, proceeds, strict=True)
    ):
        assert (row.units, row.quantity_scale, row.basis, row.realized_gain, row.source_account_id) == (
            units,
            scale,
            3 + index,
            amount - (3 + index),
            f"holding-{index}",
        )
    assert sum(row.balance for row in financial.months[-1].balances) == 0


@BUILDS
def test_recovery_cashout_applies_to_the_remaining_position_after_an_earlier_sale(
    build: Callable[[CompiledRun], World],
) -> None:
    world, financial = execute(recovery_run(2, ((3, 1), (1, 1)), earlier_sale=True), build)
    assert financial.failed_month is None
    assert (financial.dispositions[0].proceeds, financial.dispositions[0].basis) == (18, 2)
    assert [(row.units, row.basis, row.proceeds) for row in financial.dispositions[1:]] == [(1, 1, 1), (1, 4, 1)]
    assert all(lot.units_remaining == lot.basis_remaining == 0 for lot in financial.months[-1].lots)
    assert world.accounting.ledger.balance(CASH) == 20
    assert world.accounting.ledger.trial_balance() == 0


if __name__ == "__main__":
    pytest_bazel.main()
