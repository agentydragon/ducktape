"""Tests for the PE trajectory artifact reader + sampler overlay."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    assemble_level_frames,
)
from finance.augur.model.private_equity_trajectories import (
    PreSampledPrivateEquitySampler,
    PrivateEquityTrajectorySet,
    TenderEvent,
    read_private_equity_trajectories_jsonl,
)
from finance.augur.model.series import IssuerId, LevelSeriesKey, SP500Key
from util.testing.jsonl import write_jsonl


@dataclass(frozen=True)
class _MinimalSampler(Sampler):
    """A trivial Sampler that emits the typed level bundle it was constructed with —
    no PE, no other channels — so tests can isolate the overlay's behavior."""

    bundle: SampledExogenousBundle = field(
        default_factory=lambda: SampledExogenousBundle(metadata={"underlying_id": "minimal"})
    )

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return frozenset()

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        return self.bundle


def test_read_jsonl_groups_tender_events_by_issuer_and_trajectory(tmp_path: Path) -> None:
    artifact = write_jsonl(
        tmp_path / "pe.jsonl",
        [
            {
                "issuer_id": "acme",
                "trajectory_index": 0,
                "month_index": 5,
                "event_type": "tender",
                "price_per_share_usd": 100.0,
            },
            {
                "issuer_id": "acme",
                "trajectory_index": 0,
                "month_index": 18,
                "event_type": "tender",
                "price_per_share_usd": 130.0,
            },
            {
                "issuer_id": "acme",
                "trajectory_index": 1,
                "month_index": 9,
                "event_type": "tender",
                "price_per_share_usd": 90.0,
            },
            # Non-tender events are tolerated and ignored.
            {
                "issuer_id": "acme",
                "trajectory_index": 0,
                "month_index": 22,
                "event_type": "ipo",
                "price_per_share_usd": 200.0,
            },
        ],
    )
    sets = read_private_equity_trajectories_jsonl(artifact, initial_marks={"acme": 80.0})
    assert set(sets) == {"acme"}
    acme = sets["acme"]
    assert acme.initial_mark_usd == 80.0
    assert len(acme.trajectories) == 2
    assert acme.trajectories[0] == (
        TenderEvent(month_index=5, price_per_share_usd=100.0),
        TenderEvent(month_index=18, price_per_share_usd=130.0),
    )
    assert acme.trajectories[1] == (TenderEvent(month_index=9, price_per_share_usd=90.0),)


def test_read_jsonl_rejects_issuers_with_no_modeled_trajectories(tmp_path: Path) -> None:
    artifact = write_jsonl(tmp_path / "pe.jsonl", [])

    with pytest.raises(ValueError, match="no modeled trajectories"):
        read_private_equity_trajectories_jsonl(artifact, initial_marks={"holdco": 12.5})


def test_read_jsonl_rejects_unknown_issuer(tmp_path: Path) -> None:
    artifact = write_jsonl(
        tmp_path / "pe.jsonl",
        [
            {
                "issuer_id": "mystery",
                "trajectory_index": 0,
                "month_index": 1,
                "event_type": "tender",
                "price_per_share_usd": 1.0,
            }
        ],
    )
    with pytest.raises(ValueError, match="mystery"):
        read_private_equity_trajectories_jsonl(artifact, initial_marks={"acme": 100.0})


def test_sampler_overlay_emits_piecewise_constant_mark_and_event_pulse() -> None:
    """A trajectory with one tender at month 5 yields:
    - level series flat at initial_mark up to month 4, then steps to tender price for the rest
    - event series with active=True only at month 5
    """

    trajectory_set = PrivateEquityTrajectorySet(
        issuer_id="acme",
        initial_mark_usd=50.0,
        trajectories=((TenderEvent(month_index=5, price_per_share_usd=120.0),),),
    )
    sampler = PreSampledPrivateEquitySampler(
        underlying=_MinimalSampler(), trajectories_by_issuer={"acme": trajectory_set}
    )
    request = ExogenousSamplingRequest(horizon_months=10, rollout_seeds=(0,))
    bundle = sampler.sample(request)

    levels = bundle.private_equity.issuer_float_matrix("acme", "mark_usd_per_unit", rollout_count=1, horizon_months=10)
    np.testing.assert_array_equal(levels[0], np.array([50.0] * 5 + [120.0] * 6))

    events = bundle.private_equity.issuer_bool_matrix(
        "acme", "sale_opportunity_active", rollout_count=1, horizon_months=10
    )
    expected_events = np.zeros(11, dtype=np.bool_)
    expected_events[5] = True
    np.testing.assert_array_equal(events[0], expected_events)

    event_kind = bundle.private_equity.issuer_int_matrix("acme", "event_kind_code", rollout_count=1, horizon_months=10)
    np.testing.assert_array_equal(event_kind[0], np.array([0] * 5 + [1] + [0] * 5, dtype=np.int64))
    liquidity_blocked = bundle.private_equity.issuer_bool_matrix(
        "acme", "liquidity_blocked", rollout_count=1, horizon_months=10
    )
    np.testing.assert_array_equal(liquidity_blocked[0], np.zeros(11, dtype=np.bool_))


def test_sampler_overlay_cycles_trajectories_by_seed_modulo() -> None:
    """Rollout `i` picks trajectory `rollout_seeds[i] % trajectory_count`, so trajectories
    are deterministic per seed and rollouts cycle when there are more rollouts than
    trajectories."""

    trajectory_set = PrivateEquityTrajectorySet(
        issuer_id="acme",
        initial_mark_usd=10.0,
        trajectories=(
            (TenderEvent(month_index=3, price_per_share_usd=20.0),),
            (TenderEvent(month_index=4, price_per_share_usd=30.0),),
        ),
    )
    sampler = PreSampledPrivateEquitySampler(
        underlying=_MinimalSampler(), trajectories_by_issuer={"acme": trajectory_set}
    )
    request = ExogenousSamplingRequest(horizon_months=6, rollout_seeds=(0, 1, 2, 3))
    bundle = sampler.sample(request)

    events = bundle.private_equity.issuer_bool_matrix(
        "acme", "sale_opportunity_active", rollout_count=4, horizon_months=6
    )
    # seed 0 -> traj 0 -> tender at month 3
    assert events[0, 3]
    assert not events[0, 4]
    # seed 1 -> traj 1 -> tender at month 4
    assert events[1, 4]
    assert not events[1, 3]
    # seed 2 -> traj 0 (cycled)
    assert events[2, 3]
    # seed 3 -> traj 1
    assert events[3, 4]


def test_sampler_overlay_rejects_empty_trajectory_set() -> None:
    trajectory_set = PrivateEquityTrajectorySet(issuer_id="holdco", initial_mark_usd=42.0, trajectories=())
    sampler = PreSampledPrivateEquitySampler(
        underlying=_MinimalSampler(), trajectories_by_issuer={"holdco": trajectory_set}
    )
    request = ExogenousSamplingRequest(horizon_months=3, rollout_seeds=(7,))

    with pytest.raises(ValueError, match="flat private-equity fallbacks"):
        sampler.sample(request)


def test_sampler_overlay_layered_pe_bundle_uses_artifact() -> None:
    """The overlay's typed PE bundle takes precedence over any underlying PE state.

    Before the typed-series boundary the overlay also had to strip stale
    `private_equity:*` rows out of the levels frame; that path is gone now —
    `levels` only carries non-PE `LevelSeriesKey` series, so the PE mark only
    ever flows through `bundle.private_equity`.
    """

    underlying = _MinimalSampler()
    trajectory_set = PrivateEquityTrajectorySet(
        issuer_id="acme",
        initial_mark_usd=100.0,
        trajectories=((TenderEvent(month_index=2, price_per_share_usd=150.0),),),
    )
    sampler = PreSampledPrivateEquitySampler(underlying=underlying, trajectories_by_issuer={"acme": trajectory_set})
    bundle = sampler.sample(ExogenousSamplingRequest(horizon_months=5, rollout_seeds=(0,)))

    levels = bundle.private_equity.issuer_float_matrix("acme", "mark_usd_per_unit", rollout_count=1, horizon_months=5)
    np.testing.assert_array_equal(levels[0], np.array([100.0, 100.0, 150.0, 150.0, 150.0, 150.0]))


def test_sampler_overlay_preserves_underlying_non_pe_series() -> None:
    """Non-PE level series from the underlying flow through unchanged."""

    frames = assemble_level_frames([(SP500Key(), np.array([[1.0, 1.02, 1.05]]))], rollout_count=1, horizon_months=2)
    underlying = _MinimalSampler(bundle=SampledExogenousBundle(levels=frames))
    sampler = PreSampledPrivateEquitySampler(underlying=underlying, trajectories_by_issuer={})
    bundle = sampler.sample(ExogenousSamplingRequest(horizon_months=2, rollout_seeds=(0,)))

    np.testing.assert_array_equal(
        bundle.level_matrix(SP500Key(), rollout_count=1, horizon_months=2), np.array([[1.0, 1.02, 1.05]])
    )


if __name__ == "__main__":
    pytest_bazel.main()
