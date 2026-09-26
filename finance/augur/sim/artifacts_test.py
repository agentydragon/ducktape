"""The prepared-input file keeps a compiled run's exact facts and refuses anything it cannot hold exactly."""

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import InflationKey
from finance.augur.sim.artifacts import encode_prepared, read_prepared_input, write_prepared_input
from finance.augur.sim.compiler.execution import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.scenario import Agent, InitialAccountBalance, Scenario, ScheduledObligation


@pytest.fixture
def prepared() -> CompiledRun:
    """Three CPI paths and a bill whose label only the experiment defines."""
    scenario = Scenario(
        agents=[Agent(agent_id=agent) for agent in ("retiree", "world")],
        initial_cash=[
            InitialAccountBalance(agent_id="retiree", account_id="checking", balance=Decimal(100)),
            InitialAccountBalance(agent_id="world", account_id="checking", balance=Decimal(0)),
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="test-outflow",
                obligation_type="experiment:annual-outflow",
                agent_id="retiree",
                from_account_id="checking",
                to_agent_id="world",
                to_account_id="checking",
                amount_due=Decimal(150),
            )
        ],
        tax_profiles=[],
        horizon_months=13,
    )
    cpi = np.ones((3, 14))
    cpi[:, 12:] = np.asarray([2.0, 1.0, 3.0])[:, None]
    return compile_run(
        scenario,
        rollout_count=3,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(InflationKey(), cpi)], rollout_count=3, horizon_months=13
        ),
        jurisdictions={},
        locations={},
    )


def test_the_file_round_trips_the_run_exactly(prepared: CompiledRun, tmp_path: Path) -> None:
    path = tmp_path / "prepared input.json"
    write_prepared_input(prepared, path)
    decoded = read_prepared_input(path)
    assert decoded == prepared
    assert decoded.scenario.obligations[0].obligation_type == "experiment:annual-outflow"
    assert encode_prepared(decoded) == path.read_text()


@pytest.mark.parametrize(
    "invalid", ["unknown-field", "string-money", "boolean-money", "float-money", "fractional-money", "wrong-version"]
)
def test_file_decode_rejects_invalid_prepared_facts(prepared: CompiledRun, tmp_path: Path, invalid: str) -> None:
    path = tmp_path / "invalid.json"
    write_prepared_input(prepared, path)
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


if __name__ == "__main__":
    pytest_bazel.main()
