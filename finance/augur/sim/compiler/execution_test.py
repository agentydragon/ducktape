"""Checks at the scenario-to-execution-input boundary, independent of dense layouts."""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.scenario import Agent, Currency, InitialAccountBalance, Scenario


def test_compiler_uses_the_scenario_currency_quantum_for_static_money() -> None:
    scenario = Scenario(
        currency=Currency(code="CHF", quantum=Decimal("0.05")),
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance="1.25")],
        tax_profiles=[],
        horizon_months=1,
    )
    prepared = compile_run(
        scenario, rollout_count=1, external_series=ExternalSeriesContext(), jurisdictions={}, locations={}
    ).execution_input
    assert prepared["currency_code"] == "CHF"
    assert prepared["currency_quantum"] == "0.05"
    assert prepared["scenario"]["accounts"][0]["opening_balance"] == 25


def test_compiler_rejects_an_empty_population() -> None:
    scenario = Scenario(agents=[Agent(agent_id="alice")], initial_cash=[], tax_profiles=[], horizon_months=1)
    with pytest.raises(ValueError, match="rollout_count must be positive"):
        compile_run(scenario, rollout_count=0, external_series=ExternalSeriesContext(), jurisdictions={}, locations={})


if __name__ == "__main__":
    pytest_bazel.main()
