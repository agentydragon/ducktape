"""Smoke + DRY tests for the bench scenario.

The bench scenario sums up everything spike 1 added: lots, market
divergence, ordinary + capital-gains tax, floor-triggered sale,
rollout-failure detection. The tests below don't pin specific
amounts (the market is stochastic); they verify the engine runs
the configured scenario at scale without code changes.
"""

from __future__ import annotations

import polars as pl
import pytest_bazel

from augur.sim.bench_scenario import build_bench_scenario
from augur.sim.market import DeterministicPath
from augur.sim.scenario import InitialLot
from augur.sim.simulate import simulate


def test_bench_scenario_runs_at_low_rollout_count() -> None:
    """Sanity check: the full scenario completes without raising
    on a small rollout count. Pins the structural facts but not
    the (market-dependent) numbers."""
    scenario = build_bench_scenario(horizon_months=24)
    result = simulate(scenario, rollout_count=10)

    # Two recurring transfers (paycheck + rent) × 24 months × 10
    # rollouts = 480 transfer rows.
    assert result.events_log.transfers.height == 2 * 24 * 10

    # Year-end accruals: 2 jurisdictions × 2 years (months 11, 23)
    # × 10 rollouts = 40 accrual rows.
    assert result.events_log.tax_accruals.height == 2 * 2 * 10

    # Market prices: 3 assets × 25 months (0..24) × 10 rollouts.
    assert result.market_prices.height == 3 * 25 * 10

    # Rollout status frame has one row per rollout, all active or
    # failed (no other states).
    statuses = set(result.rollout_status.get_column("status").unique().to_list())
    assert statuses.issubset({"active", "failed_insufficient_cash"})


def test_dry_add_fourth_position_is_config_only() -> None:
    """Spike-1 DRY proof: adding a 4th capital-gains-eligible
    position to the scenario takes one new InitialLot record and
    one new MarketPathSpec, zero engine code. The simulator
    accepts the new asset_id ("efv") through the same code paths
    as VTI/QQQ/BTC. After running 10 rollouts at 24 months we see
    EFV as a tracked asset in lots and market-prices frames."""
    base = build_bench_scenario(horizon_months=24)
    # Append a new asset + lot. (Pydantic deep-copies via model_copy)
    new_lot = InitialLot(
        lot_id="alice_efv",
        agent_id="alice",
        asset_id="efv",
        purchase_month_index=-12,
        quantity=50.0,
        cost_basis_per_unit_usd=70.0,
    )
    efv_path = DeterministicPath(asset_id="efv", prices_usd=[100.0] * 25)
    extended = base.model_copy(
        update={
            "initial_lots": [*base.initial_lots, new_lot],
            "market": base.market.model_copy(update={"paths": [*base.market.paths, efv_path]}),
            "floor_triggered_sale_policies": [
                p.model_copy(update={"asset_preference_chain": [*p.asset_preference_chain, "efv"]})
                for p in base.floor_triggered_sale_policies
            ],
        }
    )

    result = simulate(extended, rollout_count=10)

    # The new asset shows up in lots:
    efv_lots = result.asset_lots.filter(pl.col("asset_id") == "efv")
    assert efv_lots.height > 0
    # And in market prices:
    efv_prices = result.market_prices.filter(pl.col("asset_id") == "efv")
    assert efv_prices.height == 25 * 10  # months × rollouts


if __name__ == "__main__":
    pytest_bazel.main()
