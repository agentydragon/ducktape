"""Rust/JAX differential coverage for product metrics across a frozen rollout."""

from pathlib import Path

import pytest_bazel

from finance.augur.rust.backend import run_rust_product_metric_arrays
from finance.augur.rust.testing.fixtures import feature_rich_failure_fixture, legacy_plan
from finance.augur.sim.engine.jax_engine import run_jax_product_metric_arrays


def test_rust_and_jax_match_product_metrics_across_a_rollout_failure(tmp_path: Path) -> None:
    """A frozen rollout reports the same metrics in both engines, and the failing month's
    shortfall lands in the same place — the one metric the all-funded fixture cannot show."""

    fixture = feature_rich_failure_fixture(tmp_path)
    plan = legacy_plan(fixture)

    saw_shortfall = False
    for agent_id in ("cashflow", "homeowner"):
        rust = run_rust_product_metric_arrays(fixture, primary_agent_id=agent_id)
        legacy = run_jax_product_metric_arrays(plan, primary_agent_id=agent_id)
        assert rust.failed_month.tolist() == legacy.failed_month.tolist()
        assert (rust.failed_month >= 0).all(), "the injected obligation must fail every rollout"
        rust_arrays, legacy_arrays = rust.metric_arrays(), legacy.metric_arrays()
        for name in rust_arrays:
            assert rust_arrays[name].tolist() == legacy_arrays[name].tolist(), f"{name} differs for agent {agent_id!r}"
        saw_shortfall = saw_shortfall or bool(rust_arrays["shortfall_quanta"].any())
    assert saw_shortfall


if __name__ == "__main__":
    pytest_bazel.main()
