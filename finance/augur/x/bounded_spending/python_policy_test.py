"""Compare editable Python policies to the native rule on identical compiled paths."""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import pytest_bazel

from finance.augur.rust.invocation import invoke, write_prepared_input
from finance.augur.x.bounded_spending.python_policy import (
    BatchPolicy,
    Observation,
    Observations,
    Parameters,
    ScalarAdapter,
    ScalarPolicy,
    run,
)
from finance.augur.x.bounded_spending.stress_paths import prepare
from util.bazel.runfiles import get_required_path, own_repo_rlocation


@pytest.fixture(params=[Parameters(400, 1000, 500), Parameters(400, 0, 0)])
def control(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[Parameters, str, dict[str, Any], list[dict[str, Any]]]:
    parameters: Parameters = request.param
    prepared = prepare(rollout_count=3, horizon_months=36)
    path = tmp_path / "input.json"
    write_prepared_input(prepared, path)
    output = tmp_path / "native.json"
    native = invoke(
        binary=get_required_path(own_repo_rlocation("finance/augur/x/bounded_spending/runner")),
        input_path=path,
        output_path=output,
        arguments=[str(parameters.rate_bps), str(parameters.max_cut_bps), str(parameters.max_raise_bps), "0", "1", "2"],
    )
    return (
        parameters,
        path.read_text(),
        native,
        [json.loads(output.with_suffix(f".trace-{id_}.json").read_text()) for id_ in range(3)],
    )


@pytest.mark.parametrize("batch_authored", [False, True])
@pytest.mark.parametrize("chunk_size", [None, 1, 2])
def test_scalar_and_batch_policy_match_native_controls(
    control: tuple[Parameters, str, dict[str, Any], list[dict[str, Any]]], batch_authored: bool, chunk_size: int | None
) -> None:
    parameters, input_json, native, traces = control
    ids = [0, 1, 2]
    policy = BatchPolicy(parameters, 3) if batch_authored else ScalarAdapter(parameters, ids)
    assert run(input_json, policy, ids, chunk_size=chunk_size, reverse=True) == native
    if chunk_size is None:
        for id_ in reversed(ids):
            replay_policy = BatchPolicy(parameters, 3) if batch_authored else ScalarAdapter(parameters, [id_])
            trace = run(input_json, replay_policy, [id_], forensic=True)
            assert trace["rollouts"] == [traces[id_]]
        second_year = [5_250_000, 4_500_000, 4_992_000] if parameters.max_cut_bps else [5_000_000] * 3
        assert [row[12] for row in native["consumption_paid"]] == second_year


@pytest.mark.parametrize("batch_authored", [False, True])
def test_depleted_paths_stop_and_live_zero_requests_continue(batch_authored: bool) -> None:
    input_json = json.dumps(prepare(rollout_count=3, horizon_months=36).execution_input)
    for cut, expected_length, failure in [(0, 13, 12), (10_000, 36, -1)]:
        parameters = Parameters(10_000, cut, 0)
        policy = BatchPolicy(parameters, 3) if batch_authored else ScalarAdapter(parameters, [0, 1, 2])
        result = run(input_json, policy, [0, 1, 2])
        assert result["product_metrics"]["failed_month"] == [failure] * 3
        assert all(len(path) == expected_length for path in result["consumption_paid"])
        assert all(path == [100_000_000, *([0] * (expected_length - 1))] for path in result["consumption_paid"])


@pytest.mark.parametrize("cash", [5, -5, (1 << 62) - 1])
def test_exact_rounding_uses_wide_intermediates(cash: int) -> None:
    parameters = Parameters(5000, 0, 0)
    scalar = ScalarPolicy(parameters)
    batch = BatchPolicy(parameters, 1)
    observations = Observations(
        np.array([0], dtype=np.int64),
        np.array([0], dtype=np.int64),
        np.array([cash], dtype=object),
        np.array([0], dtype=object),
        np.array([1_000_000_000], dtype=object),
    )
    expected = (abs(cash) + 1) // 2 * (-1 if cash < 0 else 1)
    assert scalar(Observation(0, cash, 0, 1_000_000_000)) == expected
    assert batch(observations) == [expected]


def test_overflow_is_not_silently_wrapped_in_batch_arithmetic() -> None:
    parameters = Parameters(400, 1000, 500)
    with pytest.raises(OverflowError):
        ScalarPolicy(parameters)(Observation(0, (1 << 63) - 1, 1, 1))
    with pytest.raises(OverflowError):
        BatchPolicy(parameters, 1)(
            Observations(
                np.array([0], dtype=np.int64),
                np.array([0], dtype=np.int64),
                np.array([(1 << 63) - 1], dtype=object),
                np.array([1], dtype=object),
                np.array([1], dtype=object),
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
