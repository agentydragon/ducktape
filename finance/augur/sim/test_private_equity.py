"""Recovery totals survive unit rounding, FIFO order and prior compulsory sales."""

import pytest
import pytest_bazel

from finance.augur.sim.capture import FinancialCapture, FinancialOutput
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedHoldingPool, PreparedLot, PreparedSeries, _TenderPolicy
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, opening
from finance.augur.sim.world import World


def recovered(total: int, positions: tuple[tuple[int, int], ...], *, earlier_sale: bool = False) -> World:
    """Each position a private lot in its own account; the issuer's recovery of `total` lands in the last month.

    With `earlier_sale`, month 0 first forces a half sale of every position.
    """
    horizon = 2 if earlier_sale else 1
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
    series = tuple(
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
    )
    world = World(MarketPath(series, 0, rollout_count=1), horizon_months=horizon)
    for account in opening({}):
        world.declare_account(account)
    for index, (_, scale) in enumerate(positions):
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=HOUSEHOLD,
                account_id=f"holding-{index}",
                asset_id="private_equity:test_issuer",
                quantity_scale=scale,
            )
        )
    for index, (units, scale) in reversed(list(enumerate(positions))):
        world.hold(
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
        )
    world.declare_tender_policy(
        _TenderPolicy(owner_agent_id=HOUSEHOLD, proceeds_account_id="checking", liquid_net_worth_floor=0)
    )
    return world


def execute(world: World) -> FinancialOutput:
    assert world.private_equity is not None
    capture = FinancialCapture(world, capture="forensic")
    world.start()
    for _ in range(world.horizon_months):
        world.close_month()
        capture.record()
        if not world.finished:
            world.open_month()
    financial = capture.financial()
    assert financial is not None
    return financial


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
    world = recovered(total, positions)
    financial = execute(world)
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
    world = recovered(2, ((3, 1), (1, 1)), earlier_sale=True)
    financial = execute(world)
    assert financial.failed_month is None
    assert (financial.dispositions[0].proceeds, financial.dispositions[0].basis) == (18, 2)
    assert [(row.units, row.basis, row.proceeds) for row in financial.dispositions[1:]] == [(1, 1, 1), (1, 4, 1)]
    assert all(lot.units_remaining == lot.basis_remaining == 0 for lot in financial.months[-1].lots)
    assert world.accounting.ledger.balance(CASH) == 20
    assert world.accounting.ledger.trial_balance() == 0


if __name__ == "__main__":
    pytest_bazel.main()
