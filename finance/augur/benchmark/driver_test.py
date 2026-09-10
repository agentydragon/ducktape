import json
import sys

import pytest
import pytest_bazel

from finance.augur.benchmark.driver import main


@pytest.mark.parametrize("output_mode", ["dense", "compact"])
def test_feature_rich_cli_runs_in_both_capture_modes(
    output_mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark", "--rollouts", "1", "--horizon-months", "60", "--repeats", "1", "--output-mode", output_mode],
    )
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["rollout_count"] == 1
    assert report["horizon_months"] == 60
    assert report["output_mode"] == output_mode
    assert len(report["warm_seconds"]) == 1
    assert report["output_bytes"] > 0
    assert len(report["output_sha256"]) == 64


if __name__ == "__main__":
    pytest_bazel.main()
