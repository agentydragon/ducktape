from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from augur.model.deterministic import Constant, Deterministic
from augur.model.exogenous import (
    SERIES_LEVELS_SCHEMA,
    SERIES_VALUES_SCHEMA,
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    series_levels_frame,
    validate_sample_satisfies_request,
)
from augur.model.gbm import GeometricBrownian
from augur.model.series import CryptoSymbol, HomeValueKey, InflationKey, LocationId, SP500Key
from augur.model.series_model import IndependentSeriesModels, SeriesModelBundle, materialize_series_values
from augur.model.testing import ConstantFrameModel


def test_scalar_models_are_owned_by_model_modules() -> None:
    assert Deterministic.__module__ == "augur.model.deterministic"
    assert Constant.__module__ == "augur.model.deterministic"
    assert GeometricBrownian.__module__ == "augur.model.gbm"


def test_sampling_request_requires_explicit_rollout_seeds() -> None:
    with pytest.raises(TypeError):
        ExogenousSamplingRequest(horizon_months=2)  # type: ignore[call-arg]

    request = ExogenousSamplingRequest(horizon_months=2, rollout_seeds=[101, 102])  # type: ignore[arg-type]
    assert request.rollout_seeds == (101, 102)
    assert request.rollout_count == 2


def test_independent_model_samples_deterministic_levels_for_each_rollout() -> None:
    # Series are grouped by typed kind: a crypto series is keyed by its symbol
    # sub-id under `crypto`, never a `"crypto:vti"` magic-prefix string. The
    # frame still carries the wire id in its `series_id` column (frame-side
    # typing is a later phase).
    model = IndependentSeriesModels(crypto={CryptoSymbol("vti"): Deterministic(levels=[100.0, 110.0, 120.0])})

    frame = model.sample(ExogenousSamplingRequest(horizon_months=2, rollout_seeds=(101, 102))).levels.sort(
        ["rollout_index", "month_index"]
    )

    assert frame.schema == SERIES_LEVELS_SCHEMA
    assert frame.to_dicts() == [
        {"rollout_index": 0, "month_index": 0, "series_id": "crypto:vti", "value": 100.0},
        {"rollout_index": 0, "month_index": 1, "series_id": "crypto:vti", "value": 110.0},
        {"rollout_index": 0, "month_index": 2, "series_id": "crypto:vti", "value": 120.0},
        {"rollout_index": 1, "month_index": 0, "series_id": "crypto:vti", "value": 100.0},
        {"rollout_index": 1, "month_index": 1, "series_id": "crypto:vti", "value": 110.0},
        {"rollout_index": 1, "month_index": 2, "series_id": "crypto:vti", "value": 120.0},
    ]


def test_bundle_api_unites_deterministic_constant_and_gbm_models() -> None:
    bundle = SeriesModelBundle.model_validate(
        {
            "model": {
                "kind": "independent",
                "crypto": {
                    "vti": {"kind": "deterministic", "levels": [100.0, 100.0, 100.0]},
                    "bnd": {"kind": "constant", "value": 95.0},
                    "qqq": {
                        "kind": "gbm",
                        "initial_value": 200.0,
                        "monthly_log_return_mu": 0.01,
                        "monthly_log_return_sigma": 0.02,
                    },
                },
            }
        }
    )

    first = materialize_series_values(bundle, rollout_seeds=(11, 12, 13), horizon_months=2)
    second = materialize_series_values(bundle, rollout_seeds=(11, 12, 13), horizon_months=2)

    assert first.schema == SERIES_VALUES_SCHEMA
    assert first.height == 27
    assert first.equals(second)
    assert first.filter((pl.col("series_id") == "crypto:qqq") & (pl.col("month_index") == 0))["value"].to_list() == [
        200.0,
        200.0,
        200.0,
    ]
    assert first.filter(pl.col("series_id") == "crypto:bnd")["value"].to_list() == [95.0] * 9


def test_deterministic_model_rejects_wrong_horizon_length() -> None:
    model = IndependentSeriesModels(crypto={CryptoSymbol("vti"): Deterministic(levels=[100.0, 110.0])})

    with pytest.raises(ValueError, match=r"need 3"):
        model.sample(ExogenousSamplingRequest(horizon_months=2, rollout_seeds=(1,)))


def test_constant_frame_fixture_samples_seeded_level_keys() -> None:
    model = ConstantFrameModel(levels={InflationKey(): 1.0, SP500Key(): 2.0})

    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=2, rollout_seeds=(101, 102), required_level_series=frozenset({InflationKey(), SP500Key()})
        )
    )

    assert sampled.level_matrix(InflationKey(), rollout_count=2, horizon_months=2).tolist() == [
        [1.0, 1.0, 1.0],
        [1.0, 1.0, 1.0],
    ]
    assert sampled.level_matrix(SP500Key(), rollout_count=2, horizon_months=2).tolist() == [
        [2.0, 2.0, 2.0],
        [2.0, 2.0, 2.0],
    ]


def test_sample_compatibility_accepts_required_subset_and_extra_series() -> None:
    request = ExogenousSamplingRequest(
        horizon_months=2, rollout_seeds=(101,), required_level_series=frozenset({SP500Key()})
    )
    sampled = SampledExogenousBundle(
        levels=pl.concat(
            [
                series_levels_frame(SP500Key(), np.ones((1, 3)), rollout_count=1, horizon_months=2),
                series_levels_frame(
                    HomeValueKey(location_id=LocationId("extra_level")),
                    np.ones((1, 3)),
                    rollout_count=1,
                    horizon_months=2,
                ),
            ]
        )
    )

    validate_sample_satisfies_request(request, sampled)


def test_sample_compatibility_rejects_missing_required_level_series() -> None:
    # A `RentKey` for an unmodeled location plays the part of an "unrecognized"
    # request key — the assertion just needs a typed `LevelSeriesKey` that the
    # empty sampled bundle won't satisfy.
    request = ExogenousSamplingRequest(
        horizon_months=2,
        rollout_seeds=(101,),
        required_level_series=frozenset({HomeValueKey(location_id=LocationId("prices_of_tea_china"))}),
    )
    sampled = SampledExogenousBundle(levels=SERIES_LEVELS_SCHEMA.to_frame())

    with pytest.raises(ValueError, match=r"missing required level series: \['home_value:prices_of_tea_china'\]"):
        validate_sample_satisfies_request(request, sampled)


if __name__ == "__main__":
    pytest_bazel.main()
