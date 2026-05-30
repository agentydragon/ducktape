"""Independent external-series model specs that feed `augur/sim`.

The simulator consumes one sampled bundle containing every modeled external
driver. Simple marginal models such as deterministic levels and GBM are
components; the model API is joint so calibrated providers can sample
correlated trajectories in one call.

Series are grouped by typed kind (inflation/sp500 singletons; crypto/home_value/
rent keyed by sub-id) via `LevelSeriesGroups` — there are no magic-prefix string
keys; the level/PE split and per-kind grouping are structural.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Annotated, Literal

import polars as pl
from pydantic import BaseModel, Field

from augur.frames import concat_frames
from augur.model.deterministic import Constant, Deterministic
from augur.model.exogenous import (
    SERIES_LEVELS_SCHEMA,
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    series_levels_frame,
    series_values_from_bundle,
)
from augur.model.gbm import GeometricBrownian
from augur.model.level_series_groups import LevelSeriesGroups
from augur.model.poisson_events import PoissonEvents
from augur.model.series import LevelSeriesKey

ScalarSeriesSpec = Annotated[Constant | Deterministic | GeometricBrownian, Field(discriminator="kind")]
ScalarEventSpec = Annotated[PoissonEvents, Field(discriminator="kind")]


class IndependentSeriesModels(LevelSeriesGroups[ScalarSeriesSpec]):
    """Joint model composed from independent per-series scalar level models.

    Inherits the per-kind level-series fields from `LevelSeriesGroups` (each maps
    to a Constant / Deterministic / GBM scalar spec). `kind` is the
    `SeriesModelSpec` discriminator.
    """

    kind: Literal["independent"] = "independent"

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        level_blocks = [
            series_levels_frame(
                key,
                model.sample_levels(
                    # Seed substreams stay keyed on the stable wire id so a series'
                    # path is identical regardless of field ordering.
                    rollout_seeds=derive_stream_rollout_seeds(request.rollout_seeds, stream_id=key.wire_id),
                    horizon_months=request.horizon_months,
                ),
                rollout_count=request.rollout_count,
                horizon_months=request.horizon_months,
            )
            for key, model in self.by_level_key().items()
        ]
        return SampledExogenousBundle(levels=concat_frames(level_blocks, SERIES_LEVELS_SCHEMA))


SeriesModelSpec = IndependentSeriesModels


class SeriesModelBundle(BaseModel):
    """A sim-facing bundle of exogenous series trajectories."""

    model: SeriesModelSpec = Field(default_factory=IndependentSeriesModels)

    @classmethod
    def independent(cls, level_series: Mapping[LevelSeriesKey, ScalarSeriesSpec]) -> SeriesModelBundle:
        return cls(model=IndependentSeriesModels.from_level_keys(level_series))

    def sample(
        self,
        *,
        horizon_months: int,
        rollout_seeds: tuple[int, ...],
        required_level_series: frozenset[LevelSeriesKey] = frozenset(),
    ) -> SampledExogenousBundle:
        model: Sampler = self.model
        return model.sample(
            ExogenousSamplingRequest(
                horizon_months=horizon_months, rollout_seeds=rollout_seeds, required_level_series=required_level_series
            )
        )


def materialize_series_values(
    bundle: SeriesModelBundle, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> pl.DataFrame:
    """Project the bundle's sampled levels into the sim external-series frame."""

    return series_values_from_bundle(bundle.sample(rollout_seeds=rollout_seeds, horizon_months=horizon_months))


def derive_stream_rollout_seeds(rollout_seeds: tuple[int, ...], *, stream_id: str) -> tuple[int, ...]:
    """Derive stable per-rollout substream seeds from a model stream id."""

    return tuple(
        int.from_bytes(hashlib.blake2b(f"{seed}:{stream_id}".encode(), digest_size=16).digest(), "big")
        for seed in rollout_seeds
    )
