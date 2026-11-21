"""Recovery totals survive unit rounding, FIFO order and prior compulsory sales."""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.sim.capture import FinancialCapture, FinancialOutput
from finance.augur.sim.ids import AccountId, AssetId, LotId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.private_equity import TenderPolicy
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, opening
from finance.augur.sim.testing.issuer_protocol import issuer_protocol
from finance.augur.sim.world import World


def recovered(total: int, positions: tuple[tuple[int, int], ...], *, earlier_sale: bool = False) -> World:
    """Each position a private lot in its own account; the issuer's recovery of `total` lands in the last month.

    With `earlier_sale`, month 0 first forces a half sale of every position.
    """
    horizon = 2 if earlier_sale else 1
    recovery = Decimal(total) / 100
    series = issuer_protocol(
        IssuerId("test_issuer"),
        horizon_months=horizon,
        mark_usd=Decimal("0.09"),
        regime=PrivateEquityRegimeCode.COLLAPSED,
        event_kind=PrivateEquityEventKindCode.FORCED_RECOVERY,
        liquidity_blocked=1,
        forced_sale=[Decimal("0.5"), 0, 0] if earlier_sale else 0,
        forced_recovery_usd=[Decimal(0), recovery, recovery] if earlier_sale else recovery,
    )
    world = World(MarketPath(series, 0, rollout_count=1), horizon_months=horizon)
    for account, balance in opening({}).items():
        world.declare_account(account=account, opening_balance=balance)
    for index, (_, scale) in enumerate(positions):
        world.declare_pool(
            agent_id=HOUSEHOLD,
            account_id=AccountId(f"holding-{index}"),
            asset_id=AssetId("private_equity:test_issuer"),
            quantity_scale=scale,
        )
    for index, (units, scale) in reversed(list(enumerate(positions))):
        world.hold_lot(
            lot_id=LotId(f"recovery-{index}"),
            agent_id=HOUSEHOLD,
            account_id=AccountId(f"holding-{index}"),
            asset_id=AssetId("private_equity:test_issuer"),
            purchase_month=-24 + index,
            quantity_scale=scale,
            units=units,
            basis=3 + index,
        )
    world.declare_tender_policy(
        TenderPolicy(owner_agent_id=HOUSEHOLD, proceeds_account_id=AccountId("checking"), liquid_net_worth_floor=0)
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
    return capture.financial()


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
