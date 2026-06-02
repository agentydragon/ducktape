"""Independent external-series model specs that feed `augur/sim`.

The simulator consumes one sampled bundle containing every modeled external
driver. Simple marginal models such as deterministic levels and GBM are
components; the model API is joint so calibrated providers can sample
correlated trajectories in one call.

Series are grouped by magisterium (asset-price sp500/crypto; property-value home_value;
index inflation/rent) via `LevelSeriesMagisteria` — there are no magic-prefix string
keys; the level/PE split and magisterium grouping are structural.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Annotated, Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, Field

from augur.frames import concat_frames
from augur.model.deterministic import Constant, Deterministic
from augur.model.exogenous import (
    ExogenousSamplingRequest,
    LevelMagisteria,
    SampledExogenousBundle,
    Sampler,
    assemble_level_magisteria,
    level_series_request_channels,
    level_value_rows,
)
from augur.model.gbm import GeometricBrownian
from augur.model.level_series_groups import (
    AssetPriceGroups,
    IndexSeriesGroups,
    LevelSeriesMagisteria,
    PropertyValueGroups,
)
from augur.model.poisson_events import PoissonEvents
from augur.model.series import LevelSeriesKey

ScalarSeriesSpec = Annotated[Constant | Deterministic | GeometricBrownian, Field(discriminator="kind")]
ScalarEventSpec = Annotated[PoissonEvents, Field(discriminator="kind")]


def sample_independent_levels(
    groups: LevelSeriesMagisteria[ScalarSeriesSpec], request: ExogenousSamplingRequest
) -> LevelMagisteria:
    """Sample every level spec across the three magisteria into the assembled level frames.

    Each magisterium is sampled from its own typed key->spec view and stays separate all the
    way into `assemble_level_magisteria`; nothing is merged into a cross-magisterium bucket.
    Seed substreams are keyed on the stable wire id so a series' path is identical regardless
    of config-dict ordering. Shared by `IndependentSeriesModels` (sim/bench) and
    `IndependentModel` (the YAML provider) — the same three magisteria, sampled once.
    """

    def blocks[KeyT: LevelSeriesKey](keyed: Mapping[KeyT, ScalarSeriesSpec]) -> list[tuple[KeyT, np.ndarray]]:
        return [
            (
                key,
                spec.sample_levels(
                    rollout_seeds=derive_stream_rollout_seeds(request.rollout_seeds, stream_id=key.wire_id),
                    horizon_months=request.horizon_months,
                ),
            )
            for key, spec in keyed.items()
        ]

    return assemble_level_magisteria(
        asset_price_blocks=blocks(groups.asset_prices.by_asset_price_key()),
        property_value_blocks=blocks(groups.property_values.by_property_value_key()),
        index_blocks=blocks(groups.index_series.by_index_series_key()),
        rollout_count=request.rollout_count,
        horizon_months=request.horizon_months,
    )


class IndependentSeriesModels(LevelSeriesMagisteria[ScalarSeriesSpec]):
    """Joint model composed from independent per-series scalar level models.

    Inherits the three magisterium sub-groups from `LevelSeriesMagisteria`
    (`asset_prices`/`property_values`/`index_series`; each series maps to a
    Constant / Deterministic / GBM scalar spec). `kind` is the `SeriesModelSpec`
    discriminator.
    """

    kind: Literal["independent"] = "independent"

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        return SampledExogenousBundle(**sample_independent_levels(self, request).as_bundle_kwargs())


SeriesModelSpec = IndependentSeriesModels


class SeriesModelBundle(BaseModel):
    """A sim-facing bundle of exogenous series trajectories."""

    model: SeriesModelSpec = Field(default_factory=IndependentSeriesModels)

    @classmethod
    def independent(
        cls,
        *,
        asset_prices: AssetPriceGroups[ScalarSeriesSpec] | None = None,
        property_values: PropertyValueGroups[ScalarSeriesSpec] | None = None,
        index_series: IndexSeriesGroups[ScalarSeriesSpec] | None = None,
    ) -> SeriesModelBundle:
        """Build an independent-model bundle from the three magisterium groups.

        Callers pass only the magisteria they populate (each defaults to empty), so the
        construction is magisterium-structured end to end — no flat `LevelSeriesKey` map
        gets partitioned back into groups.
        """

        return cls(
            model=IndependentSeriesModels(
                asset_prices=asset_prices if asset_prices is not None else AssetPriceGroups(),
                property_values=property_values if property_values is not None else PropertyValueGroups(),
                index_series=index_series if index_series is not None else IndexSeriesGroups(),
            )
        )

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
                horizon_months=horizon_months,
                rollout_seeds=rollout_seeds,
                **level_series_request_channels(required_level_series),
            )
        )


_LEGACY_SERIES_VALUES_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "series_id": pl.Utf8(), "value": pl.Float64()}
)


def materialize_series_values(
    bundle: SeriesModelBundle, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> pl.DataFrame:
    """Project the bundle's sampled levels into the sim external-series frame.

    CLEANUP: sim handoff shim until Phase 2 stage D. Rebuilds the legacy flat
    `series_id`-keyed frame from the typed per-magisterium frames by stamping
    `series_id = key.wire_id`. `augur/sim/external_series.py` is the only
    consumer; it gets retyped against the typed bundle in stage D, after which
    this shim (and the legacy schema) can be deleted.
    """

    sampled = bundle.sample(rollout_seeds=rollout_seeds, horizon_months=horizon_months)
    blocks = [
        frame.with_columns(series_id=pl.lit(key.wire_id)).select(_LEGACY_SERIES_VALUES_SCHEMA.names())
        for key, frame in level_value_rows(sampled)
    ]
    return concat_frames(blocks, _LEGACY_SERIES_VALUES_SCHEMA)


def derive_stream_rollout_seeds(rollout_seeds: tuple[int, ...], *, stream_id: str) -> tuple[int, ...]:
    """Derive stable per-rollout substream seeds from a model stream id."""

    return tuple(
        int.from_bytes(hashlib.blake2b(f"{seed}:{stream_id}".encode(), digest_size=16).digest(), "big")
        for seed in rollout_seeds
    )
