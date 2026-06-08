"""Consumer-side external-series context for the simulator.

Production evidence ingestion, model fitting, stochastic sampling, and
provenance belong in `augur/model`; `augur/sim` is a deterministic path
evaluator once it receives those trajectories.

Non-PE level series (inflation, sp500, home_value, rent, crypto) are
materialized as a long-form polars frame keyed by
`(rollout_index, month_index, series_id)` with one `value` column. The
typed PE protocol bundle is carried as `private_equity`; the sim compiler
reads PE channels directly from the bundle, no series-id translation in
the middle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from finance.augur.frames import FrameSpec
from finance.augur.model.exogenous import SampledExogenousBundle, level_value_rows
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series_model import SeriesModelBundle, materialize_series_values

# CLEANUP(2026-05-30): Phase 2 stage D retypes the sim intern table + the
# projections asset↔series join to typed keys; this flat `series_id`-string frame
# (and `series_values_from_bundle_shim`) go away then. Until then the sim keeps
# its single flat working frame, rebuilt from the bundle's per-magisterium frames.
SERIES_VALUES_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "series_id": pl.Utf8(), "value": pl.Float64()}
)


def _series_values_from_bundle_shim(bundle: SampledExogenousBundle) -> pl.DataFrame:
    """Rebuild the legacy flat `series_id`-keyed frame from per-magisterium frames.

    Stamps each per-kind frame's rows with `series_id = key.wire_id`. Sim
    handoff shim — removed with the stage-D intern/join retype.
    """

    blocks = [
        frame.with_columns(pl.lit(key.wire_id, dtype=pl.Utf8()).alias("series_id")).select(SERIES_VALUES_SCHEMA.names())
        for key, frame in level_value_rows(bundle)
    ]
    if not blocks:
        return SERIES_VALUES_SCHEMA.to_frame()
    return pl.concat(blocks, how="vertical")


EXTERNAL_SERIES_VALUES_FRAME = FrameSpec("series_values", SERIES_VALUES_SCHEMA)


@dataclass(frozen=True)
class ExternalSeriesContext:
    """The materialized external-series context.

    `series_values` carries non-PE level series (asset prices, CPI levels,
    rent levels). `private_equity` carries the typed PE protocol bundle —
    mark, regime, event-kind, fractions, blocked, recovery — per issuer;
    the sim compiler reads it directly by issuer index, no series-id
    translation in the middle. PE tender events live on the
    `private_equity.sale_opportunity_active` channel — there is no separate
    exogenous-event frame.
    """

    series_values: pl.DataFrame
    private_equity: PrivateEquityBundle = field(default_factory=PrivateEquityBundle.empty)

    def series_at(self, month_index: int) -> pl.DataFrame:
        """Cross-section view at the given month: one row per
        (rollout_index, series_id)."""
        return self.series_values.filter(pl.col("month_index") == month_index).select(
            "rollout_index", "series_id", "value"
        )


def materialize_external_series(
    bundle: SeriesModelBundle, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> ExternalSeriesContext:
    """Realize every path spec into a long-form polars frame and
    bundle it as a `ExternalSeriesContext`. The output covers months 0
    through `horizon_months` inclusive (so length `horizon_months
    + 1` per (rollout, series)). An empty bundle yields an empty
    frame with the correct schema."""
    return ExternalSeriesContext(
        series_values=EXTERNAL_SERIES_VALUES_FRAME.normalize(
            materialize_series_values(bundle, rollout_seeds=rollout_seeds, horizon_months=horizon_months)
        ),
        private_equity=PrivateEquityBundle.empty(),
    )


def materialize_sampled_exogenous(bundle: SampledExogenousBundle) -> ExternalSeriesContext:
    """Adapt a model-owned sampled bundle into the simulator's series context.

    The typed `PrivateEquityBundle` is the canonical source of PE protocol
    state — the engine reads PE channels directly from `pe_channels` arrays
    compiled out of the bundle. Non-PE level series are flattened from the
    bundle's per-magisterium frames into the sim's single working frame.
    """

    return ExternalSeriesContext(
        series_values=EXTERNAL_SERIES_VALUES_FRAME.normalize(_series_values_from_bundle_shim(bundle)),
        private_equity=bundle.private_equity,
    )
