import pytest
import pytest_bazel

from devinfra.ci.skills.ci_latency.scripts import gate_headroom


def test_clipped_last_gate_reduction_reports_remaining_headroom():
    result = gate_headroom.analyze(
        {
            "heads": [{"id": "head-a", "gates": {"bazel": 300, "artifacts": 120, "precommit": 80}}],
            "scenarios": [{"name": "large bazel cut", "reductions_seconds": {"bazel": 400}}],
        }
    )

    assert result["heads"][0]["gate_headroom_seconds"] == {"artifacts": 0, "bazel": 180, "precommit": 0}
    scenario = result["scenarios"][0]
    assert scenario["heads"][0]["scenario_wall_seconds"] == 120
    assert scenario["heads"][0]["saved_wall_seconds"] == 180
    assert scenario["remaining_last_gate_counts"] == {"artifacts": 1, "bazel": 0, "precommit": 0}


def test_noncritical_gate_reduction_saves_no_wall_time():
    result = gate_headroom.analyze(
        {
            "heads": [{"id": "head-a", "gates": {"slow": 300, "fast": 120}}],
            "scenarios": [{"name": "faster fast gate", "reductions_seconds": {"fast": 60}}],
        }
    )

    scenario = result["scenarios"][0]
    assert scenario["total_saved_wall_seconds"] == 0
    assert scenario["benefiting_heads"] == 0
    assert scenario["heads"][0]["last_gates"] == ["slow"]


def test_simultaneous_reductions_and_tied_last_gates_use_nearest_rank():
    result = gate_headroom.analyze(
        {
            "heads": [
                {"id": "head-a", "gates": {"bazel": 300, "artifacts": 300, "precommit": 80}},
                {"id": "head-b", "gates": {"bazel": 100, "artifacts": 90, "precommit": 70}},
            ],
            "scenarios": [{"name": "reduce both contenders", "reductions_seconds": {"bazel": 200, "artifacts": 200}}],
        }
    )

    assert result["last_gate_counts"] == {"artifacts": 1, "bazel": 2, "precommit": 0}
    assert result["last_gate_tied_heads"] == 1
    scenario = result["scenarios"][0]
    assert scenario["total_saved_wall_seconds"] == 230
    assert scenario["mean_saved_wall_seconds"] == 115
    assert scenario["p50_saved_wall_seconds"] == 30
    assert scenario["p90_saved_wall_seconds"] == 200
    assert scenario["remaining_last_gate_counts"] == {"artifacts": 1, "bazel": 1, "precommit": 1}
    assert scenario["remaining_last_gate_tied_heads"] == 1


@pytest.mark.parametrize(
    ("heads", "scenarios", "error_match"),
    [
        ([], [{"name": "no-op", "reductions_seconds": {}}], "heads must be a nonempty list"),
        (
            [{"id": "h", "gates": {"a": 1}}, {"id": "h", "gates": {"a": 2}}],
            [{"name": "no-op", "reductions_seconds": {}}],
            "duplicate head id",
        ),
        (
            [{"id": "h", "gates": {"a": 1}}, {"id": "j", "gates": {"b": 2}}],
            [{"name": "no-op", "reductions_seconds": {}}],
            "same complete gate set",
        ),
        (
            [{"id": "h", "gates": {"a": 1}}],
            [{"name": "bad gate", "reductions_seconds": {"missing": 1}}],
            "unknown gate",
        ),
        (
            [{"id": "h", "gates": {"a": 1}}],
            [{"name": "bad reduction", "reductions_seconds": {"a": float("nan")}}],
            "finite and nonnegative",
        ),
        (
            [{"id": "h", "gates": {"a": 1}}],
            [{"name": "negative reduction", "reductions_seconds": {"a": -1}}],
            "finite and nonnegative",
        ),
        ([{"id": "h", "gates": {"a": True}}], [{"name": "no-op", "reductions_seconds": {}}], "finite and nonnegative"),
    ],
)
def test_rejects_invalid_or_incomplete_samples(heads, scenarios, error_match):
    with pytest.raises(ValueError, match=error_match):
        gate_headroom.analyze({"heads": heads, "scenarios": scenarios})


def test_rejects_duplicate_scenario_names():
    with pytest.raises(ValueError, match="duplicate scenario name"):
        gate_headroom.analyze(
            {
                "heads": [{"id": "head-a", "gates": {"bazel": 10}}],
                "scenarios": [
                    {"name": "same", "reductions_seconds": {}},
                    {"name": "same", "reductions_seconds": {"bazel": 1}},
                ],
            }
        )


if __name__ == "__main__":
    pytest_bazel.main()
