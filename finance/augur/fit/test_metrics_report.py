"""Exercise the CLI's strict JSON boundary with the real metric battery."""

import json
from pathlib import Path

import jax.numpy as jnp
import numpy as np
import pytest
import pytest_bazel
from numpyro import distributions as dist

from finance.augur.fit.metrics_report import ModelMetricSpec, main
from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.series import InflationKey


class _TestGaussian:
    label = "test_report_gaussian"

    def fit(self, historical: HistoricalSeries) -> None:
        del historical

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution:
        del historical, t
        return dist.MultivariateNormal(jnp.zeros(1), covariance_matrix=jnp.eye(1) * horizon)


@pytest.fixture
def report_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    historical = HistoricalSeries(
        series_names=(InflationKey(),),
        levels=np.ones((11, 1)),
        months=tuple(f"2000-{month:02d}" for month in range(1, 12)),
    )
    monkeypatch.setattr("finance.augur.fit.metrics_report.load_historical", lambda: historical)
    monkeypatch.setattr(
        "finance.augur.fit.metrics_report._ACTIVE_MODEL_METRIC_SPECS",
        (ModelMetricSpec(label="test_report", build_scorable=_TestGaussian, build_fittable_scorable=_TestGaussian),),
    )


def _reject_nonstandard_number(value: str) -> None:
    raise AssertionError(f"non-standard JSON number: {value}")


@pytest.mark.usefixtures("report_inputs")
@pytest.mark.parametrize("score", [1.0, float("nan"), float("inf"), float("-inf")])
def test_cli_preserves_origins_and_explicit_uncertainty_in_strict_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, score: float
) -> None:
    monkeypatch.setattr("finance.augur.fit.metrics.joint_log_density", lambda *_: score)
    output = tmp_path / "summary.json"
    main(["--train-fraction", "0.5", "--rolling-min-train", "5", "--out", str(output)])
    text = output.read_text()
    assert capsys.readouterr().out.strip() == text
    report = json.loads(text, parse_constant=_reject_nonstandard_number)
    assert report["factor_names"] == [InflationKey().wire_id]
    assert "not significance rankings" in report["score_reporting_note"]
    rolling = report["rolling_origin"][0]
    multi_step = report["multi_step"][0]["rows"][0]
    expected = score if np.isfinite(score) else "NaN" if np.isnan(score) else "Infinity" if score > 0 else "-Infinity"
    for row in (rolling, multi_step):
        assert "joint_log_density_mean_se" not in row
        assert "Unestimated" in row["uncertainty_unestimated_reason"]
        assert [origin["origin_month"] for origin in row["origin_scores"]] == [
            "2000-06",
            "2000-07",
            "2000-08",
            "2000-09",
            "2000-10",
        ]
        assert [origin["joint_log_density"] for origin in row["origin_scores"]] == [expected] * 5
        assert row["n_origins"] == len(row["origin_scores"])
    assert rolling["joint_log_density_per_month"] == expected
    assert multi_step["joint_log_density_per_origin"] == expected


if __name__ == "__main__":
    pytest_bazel.main()
