"""Rust/JAX differential coverage for the product percentile fan and terminal distribution."""

import pytest_bazel

from finance.augur.benchmark.scenario import MIN_FEATURE_HORIZON_MONTHS, feature_rich_case
from finance.augur.product.metric_composition import METRIC_NAMES
from finance.augur.rust.backend import run_rust_product_summaries
from finance.augur.rust.differential.fixture import fixture_for
from finance.augur.sim.engine.jax_engine import run_jax_product_summaries


def test_rust_and_jax_match_product_metric_fan_and_terminal_distribution() -> None:
    """The percentile fan matches exactly, for every metric the product API can request.

    This covers the shortfall metric's own terminal reduction, which is a sum over months
    rather than the final snapshot.
    """

    case = feature_rich_case(rollout_count=8, horizon_months=MIN_FEATURE_HORIZON_MONTHS)
    plan, fixture = case.plan, fixture_for(case)
    percentiles = (5.0, 25.0, 50.0, 75.0, 95.0)

    # One agent, every metric. What varies by metric is `compose_metric` and the terminal
    # reduction, both shared Python that neither backend reimplements; what varies by agent
    # is the base series, which product_metrics_differential_test covers agent by agent.
    # A second agent here would cost another XLA compile and prove nothing new.
    for agent_id in ("homeowner",):
        for metric in METRIC_NAMES:
            rust = run_rust_product_summaries(
                fixture, primary_agent_id=agent_id, metric=metric, percentiles=percentiles
            )
            legacy = run_jax_product_summaries(plan, primary_agent_id=agent_id, metric=metric, percentiles=percentiles)
            label = f"{metric} for agent {agent_id!r}"
            assert rust.metric_fan.failed_count == legacy.metric_fan.failed_count, label
            assert rust.metric_fan.percentiles == legacy.metric_fan.percentiles, label
            assert rust.metric_fan.currency_code == legacy.metric_fan.currency_code, label
            assert rust.metric_fan.currency_quantum == legacy.metric_fan.currency_quantum, label
            assert rust.metric_fan.month_index.tolist() == legacy.metric_fan.month_index.tolist(), label
            assert rust.metric_fan.monthly_percentiles.tolist() == (legacy.metric_fan.monthly_percentiles.tolist()), (
                label
            )
            assert rust.metric_fan.terminal_percentiles.tolist() == (legacy.metric_fan.terminal_percentiles.tolist()), (
                label
            )
            assert rust.terminal_distribution.terminal_samples.tolist() == (
                legacy.terminal_distribution.terminal_samples.tolist()
            ), label
            assert rust.terminal_distribution.failed_month.tolist() == (
                legacy.terminal_distribution.failed_month.tolist()
            ), label


if __name__ == "__main__":
    pytest_bazel.main()
