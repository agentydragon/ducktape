"""Recovery totals survive unit rounding, FIFO order and prior compulsory sales."""

from dataclasses import replace

import pytest
import pytest_bazel

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


def execute(run: CompiledRun) -> World:
    world = World(run, 0, [], capture_mode="forensic", actor=None, product_actor=None)
    for month in range(run.scenario.horizon_months):
        world.prepare_month(month, {}, {})
        world.private_equity.advance(world.scenario, world.accounting, world.holdings, world.market, [], month)
        world.assemble_claims([])
        world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    return world


@pytest.mark.parametrize(
    ("total", "positions", "proceeds"),
    [
        pytest.param(1, ((3, 1),), (1,), id="total_one_for_three_units"),
        pytest.param(2, ((3, 1),), (2,), id="total_two_for_three_units"),
        pytest.param(5, ((1, 1), (2, 1), (3, 1)), (1, 2, 2), id="across_lots_and_accounts"),
        pytest.param(2, ((1, 1), (10, 10), (100, 100)), (1, 1, 0), id="economic_units_across_scales"),
    ],
)
def test_recovery_total(total: int, positions: tuple[tuple[int, int], ...], proceeds: tuple[int, ...]) -> None:
    world = execute(recovery_run(total, positions))
    financial = world.finish([]).financial
    assert financial is not None
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


def test_recovery_cashout_applies_to_the_remaining_position_after_an_earlier_sale() -> None:
    world = execute(recovery_run(2, ((3, 1), (1, 1)), earlier_sale=True))
    financial = world.finish([]).financial
    assert financial is not None
    assert financial.failed_month is None
    assert (financial.dispositions[0].proceeds, financial.dispositions[0].basis) == (18, 2)
    assert [(row.units, row.basis, row.proceeds) for row in financial.dispositions[1:]] == [(1, 1, 1), (1, 4, 1)]
    assert all(lot.units_remaining == lot.basis_remaining == 0 for lot in financial.months[-1].lots)
    assert world.accounting.ledger.balance(CASH) == 20
    assert world.accounting.ledger.trial_balance() == 0


if __name__ == "__main__":
    pytest_bazel.main()
