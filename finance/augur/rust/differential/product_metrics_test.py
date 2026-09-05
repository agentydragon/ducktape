"""Rust/JAX differential coverage for the seven base product metric series.

Its own target because JAX bakes the selected agent into the compiled program: each
agent below is a separate XLA compile of the 60-month scenario, and enough of them in
one process exhausts the test runner's memory."""

import pytest
import pytest_bazel

from finance.augur.product.metric_composition import METRIC_NAMES
from finance.augur.rust.backend import run_rust_product_metric_arrays
from finance.augur.rust.case_fixture import fixture_for
from finance.augur.sim.engine.jax_engine import run_jax_product_metric_arrays
from finance.augur.sim.testing.case import Case

# One agent per policy family the benchmark scenario separates. JAX bakes the selected agent
# into the compiled program, so each name here costs a full compile of the 60-month
# scenario — hence a covering set rather than every account holder. The metric-coverage
# assertion below is what keeps the set honest: drop an agent that uniquely carries a
# metric and the test fails rather than quietly narrowing.
PRODUCT_METRIC_AGENTS = ("allocator", "bondholder", "cashflow", "homeowner", "pe_owner")


def test_rust_and_jax_match_every_product_metric_for_every_agent(feature_rich: Case) -> None:
    """The seven base series, and everything composed from them, agree agent by agent.

    The feature-rich scenario splits policy families across agents, so it takes a set of
    them to exercise holdings, private equity, property, mortgages and bonds — no single
    agent touches every metric. Shortfall needs a failing rollout and is covered below.
    """

    plan, fixture = feature_rich.plan, fixture_for(feature_rich)

    nonzero_metrics: set[str] = set()
    for agent_id in PRODUCT_METRIC_AGENTS:
        rust = run_rust_product_metric_arrays(fixture, primary_agent_id=agent_id).metric_arrays()
        legacy = run_jax_product_metric_arrays(plan, primary_agent_id=agent_id).metric_arrays()
        assert sorted(rust) == sorted(legacy) == sorted(("month_index", *METRIC_NAMES))
        for name in rust:
            assert rust[name].tolist() == legacy[name].tolist(), f"{name} differs for agent {agent_id!r}"
            if name != "month_index" and rust[name].any():
                nonzero_metrics.add(name)

    # Without this the comparison above would pass on all-zero series. Every rollout in this
    # scenario funds every obligation, so shortfall is legitimately zero here.
    assert nonzero_metrics == set(METRIC_NAMES) - {"shortfall_quanta"}


def test_rust_product_metrics_reject_an_unknown_primary_agent(feature_rich: Case) -> None:
    with pytest.raises(ValueError, match="no account for primary agent"):
        run_rust_product_metric_arrays(fixture_for(feature_rich), primary_agent_id="nobody")


if __name__ == "__main__":
    pytest_bazel.main()
