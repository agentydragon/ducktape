import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from finance.augur.model.mirroring import MirroringSampler, MirrorLevelSeries
from finance.augur.model.series import HomeValueKey, LocationId, RentKey
from finance.augur.model.testing import ConstantFrameModel, level_matrix_with_step

_SOURCE = HomeValueKey(location_id=LocationId("vallejo_ca"))
_TARGET = HomeValueKey(location_id=LocationId("mare_island_vallejo_ca"))


def _inner_with_source() -> ConstantFrameModel:
    # A per-month-varying source so an identity copy and a re-anchor are both non-trivial.
    return ConstantFrameModel(levels={_SOURCE: level_matrix_with_step(default=500_000.0, override=600_000.0, month=1)})


def test_mirror_emits_a_copy_of_the_source_for_any_inner_model() -> None:
    sampler = MirroringSampler(
        inner=_inner_with_source(), mirror_series=(MirrorLevelSeries(target=_TARGET, source=_SOURCE),)
    )
    # The target is a first-class produced series, indistinguishable from a fitted factor.
    assert _TARGET in sampler.emittable_level_keys()

    # The consumer requires only the target; the wrapper must rewrite the inner request to the
    # source (the constant fixture raises KeyError for any key it was not seeded with).
    sampled = sampler.sample(
        ExogenousSamplingRequest(
            rollout_seeds=(7, 8), horizon_months=3, **level_series_request_channels(frozenset({_TARGET}))
        )
    )
    source_path = sampled.level_matrix(_SOURCE, rollout_count=2, horizon_months=3)
    target_path = sampled.level_matrix(_TARGET, rollout_count=2, horizon_months=3)
    np.testing.assert_array_equal(target_path, source_path)


def test_mirror_reanchors_to_initial_level_while_tracking_source_returns() -> None:
    sampler = MirroringSampler(
        inner=_inner_with_source(),
        mirror_series=(MirrorLevelSeries(target=_TARGET, source=_SOURCE, initial_level=700_000.0),),
    )
    sampled = sampler.sample(
        ExogenousSamplingRequest(
            rollout_seeds=(7, 8), horizon_months=3, **level_series_request_channels(frozenset({_SOURCE, _TARGET}))
        )
    )
    source_path = sampled.level_matrix(_SOURCE, rollout_count=2, horizon_months=3)
    target_path = sampled.level_matrix(_TARGET, rollout_count=2, horizon_months=3)
    np.testing.assert_allclose(target_path[:, 0], 700_000.0)
    np.testing.assert_allclose(target_path / target_path[:, :1], source_path / source_path[:, :1])


def test_mirror_source_must_be_emittable_by_the_inner_model() -> None:
    with pytest.raises(ValueError, match="not a fitted level factor"):
        MirroringSampler(
            inner=ConstantFrameModel(levels={}), mirror_series=(MirrorLevelSeries(target=_TARGET, source=_SOURCE),)
        )


def test_mirror_target_must_not_already_be_emittable() -> None:
    inner = ConstantFrameModel(levels={_SOURCE: 1.0, _TARGET: 1.0})
    with pytest.raises(ValueError, match="already a fitted factor"):
        MirroringSampler(inner=inner, mirror_series=(MirrorLevelSeries(target=_TARGET, source=_SOURCE),))


def test_mirror_target_and_source_must_share_a_kind() -> None:
    rent_source = RentKey(location_id=LocationId("vallejo_ca"))
    with pytest.raises(ValueError, match="must share a kind"):
        MirroringSampler(
            inner=ConstantFrameModel(levels={rent_source: 2000.0}),
            mirror_series=(MirrorLevelSeries(target=_TARGET, source=rent_source),),
        )


def test_mirror_rejects_duplicate_targets() -> None:
    other_source = HomeValueKey(location_id=LocationId("san_francisco_ca"))
    inner = ConstantFrameModel(levels={_SOURCE: 1.0, other_source: 1.0})
    with pytest.raises(ValueError, match="duplicate mirror target"):
        MirroringSampler(
            inner=inner,
            mirror_series=(
                MirrorLevelSeries(target=_TARGET, source=_SOURCE),
                MirrorLevelSeries(target=_TARGET, source=other_source),
            ),
        )


if __name__ == "__main__":
    pytest_bazel.main()
