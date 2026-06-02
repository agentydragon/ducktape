from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import pytest_bazel

from augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle, level_series_request_channels
from augur.model.sample_sanity import (
    LevelSeriesSanityCheck,
    PercentileRangeBound,
    PrivateEquityMarkSanityCheck,
    SampleSanitySpec,
    evaluate_sample_checks,
    partition_spec_coverage,
    run_sample_sanity,
    run_sample_sanity_file,
)
from augur.model.series import HomeValueKey, IssuerId, LocationId, SP500Key
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
        level_checks=(LevelSeriesSanityCheck(key=SP500Key(), value_percentile_ranges=bands),),
    )


def _sample_constant_sp500(*, horizon_months: int = _HORIZON_MONTHS) -> SampledExogenousBundle:
    model = ConstantFrameModel(levels={SP500Key(): _SP500_LEVEL})
    request = ExogenousSamplingRequest(
        horizon_months=horizon_months,
        rollout_seeds=tuple(range(_ROLLOUT_COUNT)),
        **level_series_request_channels(frozenset({SP500Key()})),
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
    assert passing_ranges
    assert all(r.observed == (_SP500_LEVEL, _SP500_LEVEL) for r in passing_ranges)


def test_evaluate_sample_checks_skips_bands_beyond_sampled_horizon() -> None:
    """A band whose month exceeds the sampled horizon is skipped, not indexed OOB."""

    far_band = PercentileRangeBound(month=120, lower_percentile=1, upper_percentile=99, lower=900.0, upper=1100.0)
    sampled = _sample_constant_sp500()
    results = evaluate_sample_checks(
        _spec_with_bands(far_band), sampled, rollout_count=_ROLLOUT_COUNT, horizon_months=_HORIZON_MONTHS
    )

    skipped = [result for result in results if result.status == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].detail == f"month 120 > sampled horizon {_HORIZON_MONTHS}"


def test_evaluate_sample_checks_surfaces_unmodeled_level_check() -> None:
    """A level check whose key the sampler can't emit returns one `unmodeled` summary row."""

    unmodeled_key = HomeValueKey(location_id=LocationId("prices_of_tea_china"))
    spec = SampleSanitySpec(
        provider_config_path=Path("unused.yaml"),
        horizon_months=_HORIZON_MONTHS,
        rollout_count=_ROLLOUT_COUNT,
        level_checks=(
            LevelSeriesSanityCheck(key=SP500Key(), value_percentile_ranges=(_PASSING_BAND,)),
            LevelSeriesSanityCheck(
                key=unmodeled_key,
                value_percentile_ranges=(_PASSING_BAND, _FAILING_BAND),
                threshold_probability_bounds=(),
            ),
        ),
    )
    model = ConstantFrameModel(levels={SP500Key(): _SP500_LEVEL})
    modeled_level, modeled_pe, unmodeled_level, unmodeled_pe = partition_spec_coverage(spec, model)
    assert modeled_level == frozenset({SP500Key()})
    assert unmodeled_level == frozenset({unmodeled_key})
    assert modeled_pe == frozenset()
    assert unmodeled_pe == frozenset()
    sampled = _sample_constant_sp500()
    results = evaluate_sample_checks(
        spec,
        sampled,
        rollout_count=_ROLLOUT_COUNT,
        horizon_months=_HORIZON_MONTHS,
        unmodeled_level_keys=unmodeled_level,
        unmodeled_pe_issuers=unmodeled_pe,
    )

    # The unmodeled level check collapses to a single summary row regardless of how many bands
    # it carried, and never indexes into the (absent) series — proves the partition gate works.
    unmodeled_rows = [result for result in results if result.status == "unmodeled"]
    assert len(unmodeled_rows) == 1
    assert unmodeled_rows[0].series_id == unmodeled_key.wire_id
    assert unmodeled_rows[0].kind == "unmodeled"
    # Other bands (against the modeled SP500 series) still surface.
    assert any(result.kind == "percentile_range" and result.status == "pass" for result in results)


def test_evaluate_sample_checks_surfaces_unmodeled_pe_mark_check() -> None:
    """A PE mark check whose issuer the sampler can't emit returns one `unmodeled` summary row."""

    spec = SampleSanitySpec(
        provider_config_path=Path("unused.yaml"),
        horizon_months=_HORIZON_MONTHS,
        rollout_count=_ROLLOUT_COUNT,
        private_equity_mark_checks=(
            PrivateEquityMarkSanityCheck(issuer_id=IssuerId("unmodeled_co"), initial_value=100.0),
        ),
    )
    model = ConstantFrameModel(levels={SP500Key(): _SP500_LEVEL})
    _, _, unmodeled_level, unmodeled_pe = partition_spec_coverage(spec, model)
    assert unmodeled_pe == frozenset({IssuerId("unmodeled_co")})
    sampled = _sample_constant_sp500()
    results = evaluate_sample_checks(
        spec,
        sampled,
        rollout_count=_ROLLOUT_COUNT,
        horizon_months=_HORIZON_MONTHS,
        unmodeled_level_keys=unmodeled_level,
        unmodeled_pe_issuers=unmodeled_pe,
    )

    assert len(results) == 1
    assert results[0].status == "unmodeled"
    assert "unmodeled_co" in results[0].series_id


def test_run_sample_sanity_raises_listing_failed_bands(tmp_path: Path) -> None:
    """The deploy gate raises an AssertionError naming every failed band."""

    provider_path = tmp_path / "provider.yaml"
    provider_path.write_text(
        dedent(f"""
            type: independent
            asset_prices:
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
    assert "2000" in message
    assert "3000" in message


if __name__ == "__main__":
    pytest_bazel.main()
