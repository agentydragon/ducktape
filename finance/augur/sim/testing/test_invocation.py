"""Prepared file transport into a Python policy and the canonical action session."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import InflationKey
from finance.augur.sim.artifacts import read_prepared_input, write_prepared_input
from finance.augur.sim.compiler.execution import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.results import Finished
from finance.augur.sim.scenario import Agent, InitialAccountBalance, Scenario
from finance.augur.sim.session import ActionSession
from finance.augur.x.bounded_spending.python_policy import BatchPolicy, Parameters, SpendingPolicy, consumption, run
from finance.augur.x.monthly_actions.run import prepare


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
    decoded = read_prepared_input(path)
    assert decoded == prepared
    parameters = Parameters(400, 0, 0)
    baseline = run(decoded, SpendingPolicy(BatchPolicy(parameters, 3), {}), [0, 1, 2])
    assert [row[12] for row in consumption(baseline)[1]] == [800, 400, 1200]
    replay = run(decoded, SpendingPolicy(BatchPolicy(parameters, 3), {}), [2, 0], capture="forensic")
    assert [row.rollout_id for row in replay.rollouts] == [2, 0]
    assert [row.summary for row in replay.rollouts] == [baseline.rollouts[id_].summary for id_ in [2, 0]]
    assert path.read_text() == encoded


@pytest.mark.parametrize(
    "invalid", ["unknown-field", "string-money", "boolean-money", "float-money", "fractional-money", "wrong-version"]
)
def test_file_decode_rejects_invalid_prepared_facts(tmp_path: Path, invalid: str) -> None:
    path = tmp_path / "invalid.json"
    write_prepared_input(prepare(), path)
    # Deliberately corrupt the external file, not a second domain representation.
    document = json.loads(path.read_text())
    if invalid == "unknown-field":
        document["scenario"]["accounts"][0]["ignored_money"] = 1
    elif invalid == "string-money":
        document["scenario"]["accounts"][0]["opening_balance"] = "100"
    elif invalid == "float-money":
        document["scenario"]["accounts"][0]["opening_balance"] = 100.0
    elif invalid == "fractional-money":
        document["scenario"]["accounts"][0]["opening_balance"] = 100.5
    elif invalid == "boolean-money":
        document["scenario"]["accounts"][0]["opening_balance"] = True
    else:
        document["schema_version"] = 999
    path.write_text(json.dumps(document))
    with pytest.raises(ValidationError):
        read_prepared_input(path)


def test_session_does_not_accept_a_parallel_raw_input_surface(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    write_prepared_input(prepare(), path)
    invalid_inputs: list[Any] = [path.read_text(), json.loads(path.read_text())]
    for raw in invalid_inputs:
        with pytest.raises(TypeError, match="CompiledRun"):
            ActionSession(raw, "example-household", [0])


def test_experiment_defined_claim_label_reaches_the_policy_after_file_loading(tmp_path: Path) -> None:
    original = prepare()
    claim = replace(original.scenario.obligations[0], obligation_type="experiment:annual-outflow")
    prepared = replace(original, scenario=replace(original.scenario, obligations=(claim,)))
    path = tmp_path / "custom-claim.json"
    write_prepared_input(prepared, path)
    session = ActionSession(read_prepared_input(path), "example-household", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        [observed] = batch[0].observation.claims
        assert observed.obligation_type == "experiment:annual-outflow"
        assert observed.amount_due == 15_000
    finally:
        session.close()


if __name__ == "__main__":
    pytest_bazel.main()
