from __future__ import annotations

import numpy as np
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
    SP500_SYMBOL,
    HomeValueKey,
    InflationKey,
    LevelSeriesKind,
    LocationId,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.model.series_model import IndependentSeriesModels, SeriesModelBundle
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
    # Series are grouped by typed kind: a security lives in the asset-price
    # role's `security` frame keyed by its `symbol` sub-id, never a
    # `"security:vti"` magic-prefix `series_id` string.
    model = IndependentSeriesModels(
        asset_prices=AssetPriceGroups(security={SecuritySymbol("vti"): Deterministic(levels=[100.0, 110.0, 120.0])})
    )

    sampled = model.sample(ExogenousSamplingRequest(horizon_months=2, rollout_seeds=(101, 102)))

    security = sampled.levels.frame(LevelSeriesKind.SECURITY).sort(["rollout_index", "month_index"])
    assert security.columns == ["rollout_index", "month_index", "symbol", "value"]
    assert security.to_dicts() == [
        {"rollout_index": 0, "month_index": 0, "symbol": "vti", "value": 100.0},
        {"rollout_index": 0, "month_index": 1, "symbol": "vti", "value": 110.0},
        {"rollout_index": 0, "month_index": 2, "symbol": "vti", "value": 120.0},
        {"rollout_index": 1, "month_index": 0, "symbol": "vti", "value": 100.0},
        {"rollout_index": 1, "month_index": 1, "symbol": "vti", "value": 110.0},
        {"rollout_index": 1, "month_index": 2, "symbol": "vti", "value": 120.0},
    ]
    np.testing.assert_allclose(
        sampled.level_matrix(SecurityKey(symbol=SecuritySymbol("vti")), rollout_count=2, horizon_months=2),
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
                    "security": {
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

    first = bundle.sample(rollout_seeds=(11, 12, 13), horizon_months=2)
    second = bundle.sample(rollout_seeds=(11, 12, 13), horizon_months=2)

    assert first.levels.series_keys() == {
        SecurityKey(symbol=SecuritySymbol(symbol)) for symbol in ("vti", "bnd", "qqq")
    }
    for key, frame in first.levels.value_rows():
        assert frame.equals(dict(second.levels.value_rows())[key])
    # The GBM component starts at its configured anchor on every rollout; the constant holds flat.
    np.testing.assert_allclose(
        first.level_matrix(SecurityKey(symbol=SecuritySymbol("qqq")), rollout_count=3, horizon_months=2)[:, 0],
        np.full(3, 200.0),
    )
    np.testing.assert_allclose(
        first.level_matrix(SecurityKey(symbol=SecuritySymbol("bnd")), rollout_count=3, horizon_months=2),
        np.full((3, 3), 95.0),
    )


def test_deterministic_model_rejects_wrong_horizon_length() -> None:
    model = IndependentSeriesModels(
        asset_prices=AssetPriceGroups(security={SecuritySymbol("vti"): Deterministic(levels=[100.0, 110.0])})
    )

    with pytest.raises(ValueError, match=r"need 3"):
        model.sample(ExogenousSamplingRequest(horizon_months=2, rollout_seeds=(1,)))


def test_constant_frame_fixture_samples_seeded_level_keys() -> None:
    model = ConstantFrameModel(levels={InflationKey(): 1.0, SecurityKey(symbol=SP500_SYMBOL): 2.0})

    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=2,
            rollout_seeds=(101, 102),
            **level_series_request_channels(frozenset({InflationKey(), SecurityKey(symbol=SP500_SYMBOL)})),
        )
    )

    assert sampled.level_matrix(InflationKey(), rollout_count=2, horizon_months=2).tolist() == [
        [1.0, 1.0, 1.0],
        [1.0, 1.0, 1.0],
    ]
    assert sampled.level_matrix(SecurityKey(symbol=SP500_SYMBOL), rollout_count=2, horizon_months=2).tolist() == [
        [2.0, 2.0, 2.0],
        [2.0, 2.0, 2.0],
    ]


def test_sample_compatibility_accepts_required_subset_and_extra_series() -> None:
    request = ExogenousSamplingRequest(
        horizon_months=2,
        rollout_seeds=(101,),
        **level_series_request_channels(frozenset({SecurityKey(symbol=SP500_SYMBOL)})),
    )
    frames = assemble_level_frames(
        [
            (SecurityKey(symbol=SP500_SYMBOL), np.ones((1, 3))),
            (HomeValueKey(location_id=LocationId("extra_level")), np.ones((1, 3))),
        ],
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
