from decimal import Decimal

import numpy as np
import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import Agent, Currency, InitialAccountBalance, Scenario, TaxProfile
from finance.augur.sim.testing.case import Case


def test_compile_run_keeps_the_supplied_paths_and_rules() -> None:
    scenario = Scenario(
        currency=Currency(code="USD", quantum=Decimal("0.01")),
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance="1.25"),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance="0"),
        ],
        tax_profiles=[TaxProfile(agent_id="alice", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")],
        horizon_months=1,
    )
    levels = np.array([[1.0, 1.02], [1.0, 0.99]])
    paths = ExternalSeriesContext.from_level_blocks([(InflationKey(), levels)], rollout_count=2, horizon_months=1)
    # An experiment-supplied rule variant must reach the plan, not be reloaded by the helper.
    jurisdictions = {
        "federal_us": load_jurisdiction("federal_us").model_copy(
            update={"standard_deduction": {"single": Decimal("123.45")}}
        )
    }
    locations = {
        "test": Location(location_id="test", display_name="Test", jurisdiction_ids=[], annual_property_tax_rate=0.0)
    }

    run = compile_run(
        scenario, rollout_count=2, external_series=paths, jurisdictions=jurisdictions, locations=locations
    )

    prepared = run.execution_input
    assert prepared["scenario"]["horizon_months"] == 1
    assert prepared["rollout_count"] == 2
    assert prepared["currency_quantum"] == "0.01"
    assert prepared["series"][0]["values"] == [1_000_000_000, 1_020_000_000, 1_000_000_000, 990_000_000]
    assert [a["opening_balance"] for a in prepared["scenario"]["accounts"]] == [125, 0]
    assert prepared["scenario"]["tax_profiles"][0]["jurisdictions"][0]["standard_deduction"] == 12345

    # The engine receives this prepared value, not mutable authoring objects to reread.
    scenario.initial_cash.clear()
    jurisdictions.clear()
    assert len(prepared["scenario"]["accounts"]) == 2
    assert prepared["scenario"]["tax_profiles"][0]["jurisdictions"][0]["standard_deduction"] == 12345


def test_case_reuses_one_prepared_run() -> None:
    case = Case(
        scenario=Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance="100")],
            tax_profiles=[],
            horizon_months=1,
        ),
        rollout_count=2,
    )

    run = case.compiled_run
    assert case.compiled_run is run


if __name__ == "__main__":
    pytest_bazel.main()
