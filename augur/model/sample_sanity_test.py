from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import pytest_bazel

from augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from augur.model.sample_sanity import (
    LevelSeriesSanityCheck,
    PercentileRangeBound,
    SampleSanitySpec,
    evaluate_sample_checks,
    run_sample_sanity,
    run_sample_sanity_file,
)
from augur.model.series import SP500Key
from augur.model.testing import ConstantFrameModel
from util.bazel.runfiles import get_required_path

_HORIZON_MONTHS = 12
_ROLLOUT_COUNT = 8
_SP500_LEVEL = 1000.0

# A passing band straddles the constant level; the failing band sits entirely above it.
_PASSING_BAND = PercentileRangeBound(month=12, lower_percentile=1, upper_percentile=99, lower=900.0, upper=1100.0)
_FAILING_BAND = PercentileRangeBound(month=12, lower_percentile=1, upper_percentile=99, lower=2000.0, upper=3000.0)


def _spec_with_bands(*bands: PercentileRangeBound) -> SampleSanitySpec:
    return SampleSanitySpec(
        provider_config_path=Path("unused.yaml"),
        horizon_months=_HORIZON_MONTHS,
        rollout_count=_ROLLOUT_COUNT,
        required_level_series=(SP500Key(),),
        level_checks=(LevelSeriesSanityCheck(key=SP500Key(), value_percentile_ranges=bands),),
    )


def _sample_constant_sp500(*, horizon_months: int = _HORIZON_MONTHS) -> SampledExogenousBundle:
    model = ConstantFrameModel(levels={SP500Key(): _SP500_LEVEL})
    request = ExogenousSamplingRequest(
        horizon_months=horizon_months,
        rollout_seeds=tuple(range(_ROLLOUT_COUNT)),
        required_level_series=frozenset({SP500Key()}),
    )
    return model.sample(request)


def test_checked_in_fixture_model_samples_sane_trajectories() -> None:
    run_sample_sanity_file(get_required_path("_main/augur/model/testdata/fixture_sample_sanity.yaml"))


def test_evaluate_sample_checks_reports_pass_and_fail() -> None:
    """A passing and a failing band against the same deterministic series both surface."""

    sampled = _sample_constant_sp500()
    results = evaluate_sample_checks(
        _spec_with_bands(_PASSING_BAND, _FAILING_BAND),
        sampled,
        rollout_count=_ROLLOUT_COUNT,
        horizon_months=_HORIZON_MONTHS,
    )

    statuses = {result.status for result in results}
    assert "pass" in statuses
    assert "fail" in statuses

    failures = [result for result in results if result.status == "fail"]
    # The above-the-level band is the only band that should fail.
    assert any("2000" in failure.detail and "3000" in failure.detail for failure in failures)
    # The passing range band records the observed percentile values it bounded.
    passing_ranges = [r for r in results if r.kind == "percentile_range" and r.status == "pass"]
    assert passing_ranges and all(r.observed == (_SP500_LEVEL, _SP500_LEVEL) for r in passing_ranges)


def test_evaluate_sample_checks_skips_bands_beyond_sampled_horizon() -> None:
    """A band whose month exceeds the sampled horizon is skipped, not indexed OOB."""

    far_band = PercentileRangeBound(month=120, lower_percentile=1, upper_percentile=99, lower=900.0, upper=1100.0)
    sampled = _sample_constant_sp500()
    results = evaluate_sample_checks(
        _spec_with_bands(far_band),
        sampled,
        rollout_count=_ROLLOUT_COUNT,
        horizon_months=_HORIZON_MONTHS,
    )

    skipped = [result for result in results if result.status == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].detail == f"month 120 > sampled horizon {_HORIZON_MONTHS}"


def test_run_sample_sanity_raises_listing_failed_bands(tmp_path: Path) -> None:
    """The deploy gate raises an AssertionError naming every failed band."""

    provider_path = tmp_path / "provider.yaml"
    provider_path.write_text(
        dedent(f"""
            type: independent
            sp500:
              kind: constant
              value: {_SP500_LEVEL}
        """),
        encoding="utf-8",
    )
    spec = _spec_with_bands(_FAILING_BAND).model_copy(update={"provider_config_path": Path("provider.yaml")})

    with pytest.raises(AssertionError) as excinfo:
        run_sample_sanity(spec, base_dir=tmp_path)
    message = str(excinfo.value)
    assert "outside expected range" in message
    assert "2000" in message and "3000" in message


if __name__ == "__main__":
    pytest_bazel.main()
