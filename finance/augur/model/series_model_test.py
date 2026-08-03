from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.deterministic import Constant, Deterministic
from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    assemble_level_frames,
    level_series_request_channels,
    validate_sample_satisfies_request,
)
from finance.augur.model.gbm import GeometricBrownian
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import (
    CryptoKey,
    CryptoSymbol,
    HomeValueKey,
    InflationKey,
    LevelSeriesKind,
    LocationId,
    SP500Key,
)
from finance.augur.model.series_model import IndependentSeriesModels, SeriesModelBundle, materialize_series_values
from finance.augur.model.testing import ConstantFrameModel


def test_scalar_models_are_owned_by_model_modules() -> None:
    assert Deterministic.__module__ == "finance.augur.model.deterministic"
    assert Constant.__module__ == "finance.augur.model.deterministic"
    assert GeometricBrownian.__module__ == "finance.augur.model.gbm"


def test_sampling_request_requires_explicit_rollout_seeds() -> None:
    with pytest.raises(TypeError):
        ExogenousSamplingRequest(horizon_months=2)  # type: ignore[call-arg]

    request = ExogenousSamplingRequest(horizon_months=2, rollout_seeds=[101, 102])  # type: ignore[arg-type]
    assert request.rollout_seeds == (101, 102)
    assert request.rollout_count == 2


def test_independent_model_samples_deterministic_levels_for_each_rollout() -> None:
    # Series are grouped by typed kind: a crypto series lives in the asset-price
    # magisterium's `crypto` frame keyed by its `symbol` sub-id, never a
    # `"crypto:vti"` magic-prefix `series_id` string.
    model = IndependentSeriesModels(
        asset_prices=AssetPriceGroups(crypto={CryptoSymbol("vti"): Deterministic(levels=[100.0, 110.0, 120.0])})
    )

    sampled = model.sample(ExogenousSamplingRequest(horizon_months=2, rollout_seeds=(101, 102)))

    crypto = sampled.levels.frame(LevelSeriesKind.CRYPTO).sort(["rollout_index", "month_index"])
    assert crypto.columns == ["rollout_index", "month_index", "symbol", "value"]
    assert crypto.to_dicts() == [
        {"rollout_index": 0, "month_index": 0, "symbol": "vti", "value": 100.0},
        {"rollout_index": 0, "month_index": 1, "symbol": "vti", "value": 110.0},
        {"rollout_index": 0, "month_index": 2, "symbol": "vti", "value": 120.0},
        {"rollout_index": 1, "month_index": 0, "symbol": "vti", "value": 100.0},
        {"rollout_index": 1, "month_index": 1, "symbol": "vti", "value": 110.0},
        {"rollout_index": 1, "month_index": 2, "symbol": "vti", "value": 120.0},
    ]
    np.testing.assert_allclose(
        sampled.level_matrix(CryptoKey(symbol=CryptoSymbol("vti")), rollout_count=2, horizon_months=2),
        np.array([[100.0, 110.0, 120.0], [100.0, 110.0, 120.0]]),
    )


def test_bundle_api_unites_deterministic_constant_and_gbm_models() -> None:
    # The GBM component is stochastic, so this asserts invariants rather than exact samples:
    # reproducibility, deterministic anchors, and materialized frame structure.
    bundle = SeriesModelBundle.model_validate(
        {
            "model": {
                "kind": "independent",
                "asset_prices": {
                    "crypto": {
                        "vti": {"kind": "deterministic", "levels": [100.0, 100.0, 100.0]},
                        "bnd": {"kind": "constant", "value": 95.0},
                        "qqq": {
                            "kind": "gbm",
                            "initial_value": 200.0,
                            "monthly_log_return_mu": 0.01,
                            "monthly_log_return_sigma": 0.02,
                        },
                    }
                },
            }
        }
    )

    first = materialize_series_values(bundle, rollout_seeds=(11, 12, 13), horizon_months=2)
    second = materialize_series_values(bundle, rollout_seeds=(11, 12, 13), horizon_months=2)

    # `materialize_series_values` is the sim-handoff shim that rebuilds the legacy
    # flat `series_id`-keyed frame from the typed per-magisterium frames.
    assert first.columns == ["rollout_index", "month_index", "series_id", "value"]
    assert first.height == 27
    assert first.equals(second)
    assert first.filter((pl.col("series_id") == "crypto:qqq") & (pl.col("month_index") == 0))["value"].to_list() == [
        200.0,
        200.0,
        200.0,
    ]
    assert first.filter(pl.col("series_id") == "crypto:bnd")["value"].to_list() == [95.0] * 9


def test_deterministic_model_rejects_wrong_horizon_length() -> None:
    model = IndependentSeriesModels(
        asset_prices=AssetPriceGroups(crypto={CryptoSymbol("vti"): Deterministic(levels=[100.0, 110.0])})
    )

    with pytest.raises(ValueError, match=r"need 3"):
        model.sample(ExogenousSamplingRequest(horizon_months=2, rollout_seeds=(1,)))


def test_constant_frame_fixture_samples_seeded_level_keys() -> None:
    model = ConstantFrameModel(levels={InflationKey(): 1.0, SP500Key(): 2.0})

    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=2,
            rollout_seeds=(101, 102),
            **level_series_request_channels(frozenset({InflationKey(), SP500Key()})),
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
        horizon_months=2, rollout_seeds=(101,), **level_series_request_channels(frozenset({SP500Key()}))
    )
    frames = assemble_level_frames(
        [(SP500Key(), np.ones((1, 3))), (HomeValueKey(location_id=LocationId("extra_level")), np.ones((1, 3)))],
        rollout_count=1,
        horizon_months=2,
    )
    sampled = SampledExogenousBundle(levels=frames)

    validate_sample_satisfies_request(request, sampled)


def test_sample_compatibility_rejects_missing_required_level_series() -> None:
    # A `RentKey` for an unmodeled location plays the part of an "unrecognized"
    # request key — the assertion just needs a typed `LevelSeriesKey` that the
    # empty sampled bundle won't satisfy.
    request = ExogenousSamplingRequest(
        horizon_months=2,
        rollout_seeds=(101,),
        **level_series_request_channels(frozenset({HomeValueKey(location_id=LocationId("prices_of_tea_china"))})),
    )
    sampled = SampledExogenousBundle()

    with pytest.raises(ValueError, match=r"missing required level series: \['home_value:prices_of_tea_china'\]"):
        validate_sample_satisfies_request(request, sampled)


if __name__ == "__main__":
    pytest_bazel.main()
