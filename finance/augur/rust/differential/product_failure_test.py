"""Rust/JAX differential coverage for product metrics across a frozen rollout."""

from pathlib import Path
from typing import Any

import pytest_bazel

from finance.augur.rust.backend import run_rust_product_metric_arrays
from finance.augur.rust.differential.fixtures import feature_rich_fixture, legacy_plan
from finance.augur.sim.engine.jax_engine import run_jax_product_metric_arrays


def feature_rich_failure_fixture(tmp_path: Path) -> dict[str, Any]:
    """The feature-rich fixture with one obligation nobody can fund.

    Building the failure case from the full fixture rather than a bare one keeps the
    exogenous series every metric reads, so the comparison still covers holdings, property
    and bonds on the frozen side of the failure.
    """

    fixture = feature_rich_fixture(tmp_path)
    fixture["scenario"]["obligations"].append(
        {
            "month": 30,
            "obligation_id": "unfundable-differential-probe",
            "from": {"agent_id": "cashflow", "account_id": "checking"},
            "to": {"agent_id": "vendor", "account_id": "checking"},
            "amount_due": 10_000_000_000,
        }
    )
    return fixture


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
