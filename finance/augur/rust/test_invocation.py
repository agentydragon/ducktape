"""Shared file transport through a real experiment and canonical execution."""

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.rust.invocation import invoke, write_prepared_input
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.scenario import Agent, InitialAccountBalance, Scenario
from util.bazel.runfiles import get_required_path


@pytest.fixture
def binary() -> Path:
    return get_required_path("_main/finance/augur/x/bounded_spending/runner")


@pytest.fixture
def input_path(tmp_path: Path) -> Path:
    # These account identities are the bounded-spending example's declared bindings.
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
    run = compile_run(
        scenario,
        rollout_count=3,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(InflationKey(), cpi)], rollout_count=3, horizon_months=13
        ),
        jurisdictions={},
        locations={},
    )
    path = tmp_path / "prepared input.json"
    write_prepared_input(run, path)
    return path


def test_reordered_selected_replays_keep_original_ids_and_fresh_state(
    binary: Path, input_path: Path, tmp_path: Path
) -> None:
    prepared = input_path.read_bytes()
    first = invoke(
        binary=binary,
        input_path=input_path,
        output_path=tmp_path / "first output.json",
        arguments=["400", "0", "0", "2", "0"],
    )
    second = invoke(
        binary=binary,
        input_path=input_path,
        output_path=tmp_path / "second output.json",
        arguments=["400", "0", "0", "0", "2"],
    )
    assert first == second
    assert input_path.read_bytes() == prepared
    assert first["product_metrics"]["failed_month"] == [-1, -1, -1]
    assert [path[12] for path in first["consumption_paid"]] == [800, 400, 1200]
    for rollout_id in (2, 0):
        trace = json.loads((tmp_path / f"first output.trace-{rollout_id}.json").read_text())
        replay = json.loads((tmp_path / f"second output.trace-{rollout_id}.json").read_text())
        assert trace == replay
        assert trace["rollout_id"] == rollout_id
        assert [row["amount_paid"] for row in trace["obligations"]] == [400, [800, 400, 1200][rollout_id]]
    assert not (tmp_path / "first output.trace-1.json").exists()


@pytest.mark.parametrize("arguments", [[], ["not-a-rate", "0", "0"], ["400", "0", "0", "3"]])
def test_parameter_and_replay_errors_propagate(
    binary: Path, input_path: Path, tmp_path: Path, arguments: list[str]
) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        invoke(binary=binary, input_path=input_path, output_path=tmp_path / "output.json", arguments=arguments)


@pytest.mark.parametrize("contents", [None, "{invalid", "{}"])
def test_input_file_and_deserialization_errors_propagate(binary: Path, tmp_path: Path, contents: str | None) -> None:
    path = tmp_path / "invalid-input.json"
    if contents is not None:
        path.write_text(contents)
    with pytest.raises(subprocess.CalledProcessError):
        invoke(binary=binary, input_path=path, output_path=tmp_path / "output.json", arguments=["400", "0", "0"])
    assert not (tmp_path / "output.json").exists()


def test_output_write_failure_propagates(binary: Path, input_path: Path, tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        invoke(
            binary=binary,
            input_path=input_path,
            output_path=tmp_path / "absent-directory" / "output.json",
            arguments=["400", "0", "0"],
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [("[]", "JSON object"), ("42", "JSON object"), ("null", "JSON object"), ("{invalid", "Expecting property name")],
)
def test_successful_runner_must_produce_an_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str, message: str
) -> None:
    # Corrupt output at the external process boundary; real native execution is above.
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    output = tmp_path / "invalid-output.json"
    output.write_text(contents)
    with pytest.raises(ValueError, match=message):
        invoke(binary=Path("test-runner"), input_path=tmp_path / "input.json", output_path=output, arguments=[])


if __name__ == "__main__":
    pytest_bazel.main()
