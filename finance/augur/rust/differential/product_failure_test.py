"""Rust/JAX differential coverage for product metrics across a frozen rollout."""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.benchmark.scenario import MIN_FEATURE_HORIZON_MONTHS, feature_rich_case
from finance.augur.rust.backend import run_rust_product_metric_arrays
from finance.augur.rust.case_fixture import fixture_for
from finance.augur.sim.engine.jax_engine import run_jax_product_metric_arrays
from finance.augur.sim.scenario import ObligationType, ScheduledObligation
from finance.augur.sim.testing.case import Case


@pytest.fixture
def feature_rich_failure() -> Case:
    """The feature-rich case with one obligation nobody can fund.

    Building the failure case from the full scenario rather than a bare one keeps the
    exogenous series every metric reads, so the comparison still covers holdings, property
    and bonds on the frozen side of the failure.
    """

    return feature_rich_case(
        rollout_count=4,
        horizon_months=MIN_FEATURE_HORIZON_MONTHS,
        extra_obligations=[
            ScheduledObligation(
                month=30,
                obligation_id="unfundable-differential-probe",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id="cashflow",
                from_account_id="checking",
                to_agent_id="vendor",
                to_account_id="checking",
                amount_due=Decimal(100_000_000),
            )
        ],
    )


def test_rust_and_jax_match_product_metrics_across_a_rollout_failure(feature_rich_failure: Case) -> None:
    """A frozen rollout reports the same metrics in both engines, and the failing month's
    shortfall lands in the same place — the one metric the all-funded scenario cannot show."""

    plan, fixture = feature_rich_failure.plan, fixture_for(feature_rich_failure)

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


def test_a_frozen_rollout_reports_its_property_value_as_net_worth(feature_rich_failure: Case) -> None:
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

    metrics = run_jax_product_metric_arrays(feature_rich_failure.plan, primary_agent_id="homeowner").metric_arrays()
    final = {name: values[-1] for name, values in metrics.items() if name != "month_index"}

    for drained in ("cash_quanta", "holding_value_quanta", "mortgage_balance_quanta", "bond_value_quanta"):
        assert not final[drained].any(), f"{drained} should be drained by the freeze"
    assert final["property_value_quanta"].any(), "the property term survives the freeze"
    assert final["net_worth_quanta"].tolist() == final["property_value_quanta"].tolist()


if __name__ == "__main__":
    pytest_bazel.main()
