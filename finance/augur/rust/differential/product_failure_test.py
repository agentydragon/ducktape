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


def test_a_frozen_rollout_reports_its_property_value_as_net_worth(tmp_path: Path) -> None:
    """Pins the uneven failure semantics both engines share.

    Failure drains dollar-valued state, and the bond term is zeroed explicitly so that "a
    failed rollout's net worth is zero like every other term". A property's metric value is
    `purchase_price x home_value[now] / home_value[purchase_month]` — both terms static or
    exogenous — and the property's active flag survives the freeze, so that term is not
    zeroed and net worth ends up equal to it.

    This looks like an oversight rather than a rule, but it is what the product reports
    today and what the Rust engine matches. If the property term is zeroed on failure, this
    test fails, which is the signal to update it rather than a regression.
    """

    fixture = feature_rich_failure_fixture(tmp_path)
    metrics = run_jax_product_metric_arrays(legacy_plan(fixture), primary_agent_id="homeowner").metric_arrays()
    final = {name: values[-1] for name, values in metrics.items() if name != "month_index"}

    for drained in ("cash_quanta", "holding_value_quanta", "mortgage_balance_quanta", "bond_value_quanta"):
        assert not final[drained].any(), f"{drained} should be drained by the freeze"
    assert final["property_value_quanta"].any(), "the property term survives the freeze"
    assert final["net_worth_quanta"].tolist() == final["property_value_quanta"].tolist()


if __name__ == "__main__":
    pytest_bazel.main()
