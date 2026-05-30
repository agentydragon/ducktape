"""Smoke + DRY tests for the bench scenario.

The bench scenario sums up everything spike 1 added: lots, exogenous
divergence, ordinary + capital-gains tax, liquidity-policy sale,
rollout-failure detection. The tests below don't pin specific
amounts (the fixture path source is stochastic); they verify the
engine runs the configured scenario at scale without code changes.
"""

from __future__ import annotations

import polars as pl
import pytest_bazel

from augur.model.deterministic import Deterministic
from augur.model.series import CryptoKey, CryptoSymbol
from augur.model.series_model import SeriesModelBundle
from augur.sim.bench_scenario import build_bench_scenario
from augur.sim.scenario import InitialLot
from augur.sim.simulate import simulate


def test_bench_scenario_runs_at_low_rollout_count() -> None:
    """Sanity check: the full scenario completes without raising
    on a small rollout count. Pins the structural facts but not
    the path-dependent numbers."""
    scenario = build_bench_scenario(horizon_months=24)
    result = simulate(scenario, rollout_count=10, locations={})

    # Two recurring transfers (paycheck + rent) x 24 months x 10
    # rollouts = 480 monthly transfer rows. Tax payment timing is
    # scenario-dependent, so only assert the recurring spine here.
    assert result.events_log.transfers.height >= 2 * 24 * 10
    assert result.events_log.transfers.filter(pl.col("cause_id").str.contains("tax")).height > 0

    # Year-end accruals: 2 jurisdictions x 2 years (months 11, 23)
    # x 10 rollouts = 40 accrual rows.
    assert result.events_log.tax_accruals.height == 2 * 2 * 10

    # External series values: 3 assets x 25 months (0..24) x 10 rollouts.
    assert result.series_values.height == 3 * 25 * 10

    # Rollout status frame has one row per rollout, all active or
    # failed (no other states).
    statuses = set(result.rollout_status.get_column("status").unique().to_list())
    assert statuses.issubset({"active", "failed_insufficient_cash"})


def test_dry_add_fourth_position_is_config_only() -> None:
    """Spike-1 DRY proof: adding a 4th capital-gains-eligible
    position to the scenario takes one new InitialLot record and
    one new independent external-series entry, zero engine code. The simulator
    accepts the new asset_id ("crypto:efv") through the same code paths
    as VTI/QQQ/BTC. After running 10 rollouts at 24 months we see
    EFV as a tracked asset in lots and external-series frames."""
    base = build_bench_scenario(horizon_months=24)
    # Append a new asset + lot. (Pydantic deep-copies via model_copy)
    new_lot = InitialLot(
        lot_id="alice_efv",
        agent_id="alice",
        asset_id="crypto:efv",
        purchase_month_index=-12,
        quantity=50.0,
        cost_basis_per_unit_usd=70.0,
    )
    series = {
        **base.external_series.model.by_level_key(),
        CryptoKey(symbol=CryptoSymbol("efv")): Deterministic(levels=[100.0] * 25),
    }
    extended = base.model_copy(
        update={
            "initial_lots": [*base.initial_lots, new_lot],
            "external_series": SeriesModelBundle.independent(series),
            "liquidity_policies": [
                p.model_copy(update={"asset_preference_chain": [*p.asset_preference_chain, "crypto:efv"]})
                for p in base.liquidity_policies
            ],
        }
    )

    result = simulate(extended, rollout_count=10, locations={})

    # The new asset shows up in lots:
    efv_lots = result.asset_lots.filter(pl.col("asset_id") == "crypto:efv")
    assert efv_lots.height > 0
    # And in external series values:
    efv_values = result.series_values.filter(pl.col("series_id") == "crypto:efv")
    assert efv_values.height == 25 * 10  # months x rollouts


if __name__ == "__main__":
    pytest_bazel.main()
