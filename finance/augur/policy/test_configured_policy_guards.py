"""What `ConfiguredHousehold.check` refuses in a configured allocation policy before it is tracked.

These are guards on the policy record against the world it would act on, not on financial
execution; the financial behaviour of the same policies lives in
<../sim/allocation_household_test.py>.
"""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.books import AccountRef
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedIndexedAmount,
    PreparedLot,
    PreparedSeries,
    _AllocationPolicy,
    _SleeveTarget,
)
from finance.augur.sim.world import World

ALICE = "test-alice"
STOCK = "test-stock"
SCALE = 1_000_000
HORIZON = 13
POLICY = _AllocationPolicy(
    agent_id=ALICE,
    account_id="checking",
    source_account_ids=("brokerage",),
    sleeves=(_SleeveTarget(asset_id=STOCK, weight=1, quantity_scale=SCALE),),
    cash_floor=0,
    cash_ceiling=0,
    cause_id_prefix="fund",
    allow_purchases=True,
    rebalance_tolerance_ppb=None,
)
INDEX = PreparedIndexedAmount(base_amount=0, series_id="inflation", base_month_index=0, adjustment_period_months=12)


def holding(*, lot_id: str = "opening-stock", inflation: tuple[int, ...] | None = None) -> World:
    """Alice's cash and 100 shares held in brokerage, on a flat price path and optionally a CPI path."""
    series = [PreparedSeries(series_id=f"security:{STOCK}", snapshots=HORIZON + 1, values=(1_000,) * (HORIZON + 1))]
    if inflation is not None:
        series.append(PreparedSeries(series_id="inflation", snapshots=HORIZON + 1, values=inflation))
    world = World(MarketPath(series, 0, rollout_count=1), horizon_months=HORIZON)
    world.declare_account(
        PreparedAccount(account=AccountRef(agent_id=ALICE, account_id="checking"), opening_balance=10_000)
    )
    world.declare_pool(
        PreparedHoldingPool(agent_id=ALICE, account_id="brokerage", asset_id=STOCK, quantity_scale=SCALE)
    )
    world.hold(
        PreparedLot(
            lot_id=lot_id,
            agent_id=ALICE,
            account_id="brokerage",
            asset_id=STOCK,
            purchase_month=-24,
            quantity_scale=SCALE,
            units=100 * SCALE,
            basis=50_000,
        )
    )
    return world


def check(world: World, *policies: _AllocationPolicy) -> None:
    ConfiguredHousehold(AgentId(ALICE), policies).check(world)


def test_generated_purchase_namespace_is_reserved() -> None:
    reserved = holding(lot_id="fund_buy_p0_s0_1000000")
    with pytest.raises(ValueError, match="reserved allocation-purchase identity"):
        check(reserved, POLICY)
    check(reserved, replace(POLICY, allow_purchases=False))
    check(holding(lot_id="fund_buy_p0_s0_1000000x"), POLICY)


@pytest.mark.parametrize(
    ("policies", "error"),
    [
        ((replace(POLICY, source_account_ids=("brokerage", "brokerage")),), "source accounts must be unique"),
        ((replace(POLICY, source_account_ids=("undeclared",)),), "purchase pool is not declared"),
        (
            (replace(POLICY, sleeves=(_SleeveTarget(asset_id=STOCK, weight=1, quantity_scale=10),)),),
            "quantity grid disagrees",
        ),
        (
            (replace(POLICY, sleeves=(_SleeveTarget(asset_id=STOCK, weight=1, quantity_scale=3),)),),
            "power-of-ten quantity grid",
        ),
        ((replace(POLICY, account_id="undeclared"),), "declared funding account"),
        ((replace(POLICY, cause_id_prefix=" "),), "nonempty cause"),
        ((POLICY, POLICY), "duplicate allocation funding account"),
        ((replace(POLICY, sleeves=POLICY.sleeves * 2),), "duplicate allocation sleeve"),
        (
            (replace(POLICY, sleeves=(_SleeveTarget(asset_id=STOCK, weight=0, quantity_scale=SCALE),)),),
            "positive target",
        ),
        ((replace(POLICY, sleeves=(_SleeveTarget(asset_id=STOCK, weight=-1, quantity_scale=SCALE),)),), "nonnegative"),
        ((replace(POLICY, allow_purchases=False, rebalance_tolerance_ppb=0),), "drift requires purchases"),
        ((replace(POLICY, cash_floor=1),), "must not exceed"),
        ((replace(POLICY, cash_ceiling=replace(INDEX, adjustment_period_months=0)),), "invalid base month or reset"),
        ((replace(POLICY, cash_ceiling=replace(INDEX, base_month_index=1)),), "starts before its base month"),
        ((replace(POLICY, cash_ceiling=INDEX),), "missing series"),
    ],
    ids=[
        "sources",
        "purchase_pool",
        "source_grid",
        "invalid_grid",
        "funding",
        "cause",
        "duplicate_policy",
        "duplicate_sleeve",
        "zero_weights",
        "negative_weight",
        "disabled_drift",
        "band",
        "period",
        "base_month",
        "missing_index",
    ],
)
def test_a_malformed_policy_is_refused_before_the_household_is_tracked(
    policies: tuple[_AllocationPolicy, ...], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        check(holding(), *policies)


def test_an_indexed_bound_needs_a_positive_level_at_every_reset_it_reads() -> None:
    with pytest.raises(ValueError, match="positive index levels"):
        check(holding(inflation=(10**9,) * 12 + (0, 0)), replace(POLICY, cash_ceiling=INDEX))


def test_exact_integer_indices_and_a_sales_only_scope_are_valid() -> None:
    # An exact i64 index above 2**53 is valid; absent purchase destinations remain valid for sales-only rules.
    check(
        holding(inflation=(2**53 + 1,) * (HORIZON + 1)),
        replace(POLICY, allow_purchases=False, source_account_ids=("unused-holdings",), cash_ceiling=INDEX),
    )


if __name__ == "__main__":
    pytest_bazel.main()
