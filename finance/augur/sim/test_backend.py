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

    assert run.scenario is scenario
    assert run.external_series is paths
    assert run.jurisdictions is jurisdictions
    assert run.locations is locations
    assert run.plan.horizon_months == 1
    assert run.plan.rollout_count == 2
    assert run.plan.currency_quantum == Decimal("0.01")
    np.testing.assert_array_equal(run.plan.external_values[0], levels)
    np.testing.assert_array_equal(run.plan.cash_initial_balance, [125, 0, 0])
    np.testing.assert_array_equal(run.plan.tax.link_standard_deduction, [12345])


def test_case_plan_and_run_share_one_compilation() -> None:
    case = Case(
        scenario=Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance="100")],
            tax_profiles=[],
            horizon_months=1,
        ),
        rollout_count=2,
    )

    plan = case.plan
    assert case.compiled_run.plan is plan
    assert case.plan is plan
    assert case.compiled_run.external_series is case.external_series


if __name__ == "__main__":
    pytest_bazel.main()
