"""Prepared file transport into a Python policy and the canonical action session."""

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.rust.invocation import write_prepared_input
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.scenario import Agent, InitialAccountBalance, Scenario
from finance.augur.x.bounded_spending.python_policy import BatchPolicy, Parameters, SpendingPolicy, consumption, run


def test_prepared_input_retains_original_path_cpi_and_selected_replay(tmp_path: Path) -> None:
    scenario = Scenario(
        agents=[Agent(agent_id=agent) for agent in ("retiree", "world")],
        initial_cash=[
            InitialAccountBalance(agent_id="retiree", account_id="checking", balance=Decimal(100)),
            InitialAccountBalance(agent_id="world", account_id="checking", balance=Decimal(0)),
        ],
        tax_profiles=[],
        horizon_months=13,
    )
    cpi = np.ones((3, 14))
    cpi[:, 12:] = np.asarray([2.0, 1.0, 3.0])[:, None]
    prepared = compile_run(
        scenario,
        rollout_count=3,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(InflationKey(), cpi)], rollout_count=3, horizon_months=13
        ),
        jurisdictions={},
        locations={},
    )
    path = tmp_path / "prepared input.json"
    write_prepared_input(prepared, path)
    encoded = path.read_text()
    assert json.loads(encoded) == prepared.execution_input
    parameters = Parameters(400, 0, 0)
    baseline = run(encoded, SpendingPolicy(BatchPolicy(parameters, 3), {}), [0, 1, 2])
    assert [row[12] for row in consumption(baseline)[1]] == [800, 400, 1200]
    replay = run(encoded, SpendingPolicy(BatchPolicy(parameters, 3), {}), [2, 0], capture="forensic")
    assert [row.rollout_id for row in replay.rollouts] == [2, 0]
    assert [row.summary for row in replay.rollouts] == [baseline.rollouts[id_].summary for id_ in [2, 0]]
    assert path.read_text() == encoded


if __name__ == "__main__":
    pytest_bazel.main()
