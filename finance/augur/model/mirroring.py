"""Model-level series mirroring: emit a target series as a copy of another series.

`MirroringSampler` wraps any exogenous `Sampler` (the way `CompositeModel` composes
macro + PE) and declares that one series is, for now, the same market as another: the
inner model fits `source`, and the wrapper emits `target` as a per-rollout copy of the
sampled `source` path. Use it for a sub-area that shares its city's price/rent index when
we lack the data to model them apart. Because `target` becomes a first-class produced
series — indistinguishable from a fitted factor to calibration, projection, and product —
swapping in a real fitted `source` later needs zero downstream change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from pydantic import Field

from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    anchor_sampled_series_levels,
    assemble_level_frames,
    merge_level_frames,
    validate_sample_satisfies_request,
)
from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import IssuerId, LevelSeriesKey


class MirrorLevelSeries(FrozenModel):
    """Emit `target` as a per-rollout copy of the produced `source` series' sampled path.

    `source` must be emittable by the wrapped model and share `target`'s kind.
    `initial_level=None` shares the source's level exactly; otherwise the shared return
    path is re-anchored per rollout to start at `initial_level`."""

    target: LevelSeriesKey
    source: LevelSeriesKey
    initial_level: float | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class MirroringSampler:
    """Wrap a `Sampler`, emitting each mirror `target` as a copy of the inner `source` series."""

    inner: Sampler
    mirror_series: tuple[MirrorLevelSeries, ...]

    def __post_init__(self) -> None:
        emittable = self.inner.emittable_level_keys()
        seen_targets: set[LevelSeriesKey] = set()
        for mirror in self.mirror_series:
            if mirror.source not in emittable:
                raise ValueError(f"mirror source {mirror.source.wire_id!r} is not a fitted level factor")
            if mirror.target in emittable:
                raise ValueError(f"mirror target {mirror.target.wire_id!r} is already a fitted factor")
            if mirror.target.kind != mirror.source.kind:
                raise ValueError(
                    f"mirror target {mirror.target.wire_id!r} and source {mirror.source.wire_id!r} must share a kind"
                )
            if mirror.target in seen_targets:
                raise ValueError(f"duplicate mirror target {mirror.target.wire_id!r}")
            seen_targets.add(mirror.target)

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return self.inner.emittable_level_keys() | {mirror.target for mirror in self.mirror_series}

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return self.inner.emittable_private_equity_issuers()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        bundle = self.inner.sample(self._inner_request(request))
        rollout_count = request.rollout_count
        horizon_months = request.horizon_months
        target_blocks = [
            (
                mirror.target,
                bundle.level_matrix(mirror.source, rollout_count=rollout_count, horizon_months=horizon_months),
            )
            for mirror in self.mirror_series
        ]
        mirror_levels = assemble_level_frames(target_blocks, rollout_count=rollout_count, horizon_months=horizon_months)
        merged = SampledExogenousBundle(
            levels=merge_level_frames(bundle.levels, mirror_levels),
            private_equity=bundle.private_equity,
            metadata=bundle.metadata,
        )
        anchors = {
            mirror.target: mirror.initial_level for mirror in self.mirror_series if mirror.initial_level is not None
        }
        if anchors:
            merged = anchor_sampled_series_levels(merged, level_series_anchors=anchors)
        validate_sample_satisfies_request(request, merged)
        return merged

    def _inner_request(self, request: ExogenousSamplingRequest) -> ExogenousSamplingRequest:
        # A consumer may require a mirror target, but the inner model only knows the source.
        # Rewrite each required target to its source; source and target share a kind, so each
        # key stays in its own magisterium request channel.
        source_by_target: dict[LevelSeriesKey, LevelSeriesKey] = {
            mirror.target: mirror.source for mirror in self.mirror_series
        }

        def to_sources[KeyT: LevelSeriesKey](keys: frozenset[KeyT]) -> frozenset[KeyT]:
            return frozenset(cast("KeyT", source_by_target.get(key, key)) for key in keys)

        return ExogenousSamplingRequest(
            horizon_months=request.horizon_months,
            rollout_seeds=request.rollout_seeds,
            required_asset_prices=to_sources(request.required_asset_prices),
            required_property_values=to_sources(request.required_property_values),
            required_index_series=to_sources(request.required_index_series),
            required_private_equity_issuers=request.required_private_equity_issuers,
        )
