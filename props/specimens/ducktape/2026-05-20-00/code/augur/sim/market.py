"""Consumer-side market context for the simulator.

The simulator consumes materialized per-(asset, rollout, month) price
paths. Production evidence ingestion, model fitting, stochastic
sampling, and provenance belong in `augur/model`; `augur/sim` is a
deterministic path evaluator once it receives those trajectories.

The materialized bundle is a long-form polars frame keyed by
`(rollout_index, month_index, asset_id)` with one column
`price_per_unit_usd`. Subsequent step calls index into it by month.

The joint market model specs are owned by `augur.model.market`.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from augur.frames import FrameSpec
from augur.model.market import MarketBundle, materialize_market_prices
from augur.model.market_api import MARKET_PRICES_SCHEMA, SampledMarketBundle, market_prices_from_levels

MARKET_PRICES_FRAME = FrameSpec("market_prices", MARKET_PRICES_SCHEMA)


@dataclass(frozen=True)
class MarketContext:
    """The materialized market frame plus quick filtered views.
    Construct once at sim start; pass alongside `state` into step
    calls. The frame schema is `MARKET_PRICES_SCHEMA`."""

    prices: pl.DataFrame

    def prices_at(self, month_index: int) -> pl.DataFrame:
        """Cross-section view at the given month: one row per
        (rollout_index, asset_id)."""
        return self.prices.filter(pl.col("month_index") == month_index).select(
            "rollout_index", "asset_id", "price_per_unit_usd"
        )


def materialize_market(bundle: MarketBundle, *, rollout_seeds: tuple[int, ...], horizon_months: int) -> MarketContext:
    """Realize every path spec into a long-form polars frame and
    bundle it as a `MarketContext`. The output covers months 0
    through `horizon_months` inclusive (so length `horizon_months
    + 1` per (rollout, asset)). An empty bundle yields an empty
    frame with the correct schema."""
    return MarketContext(
        prices=MARKET_PRICES_FRAME.normalize(
            materialize_market_prices(bundle, rollout_seeds=rollout_seeds, horizon_months=horizon_months)
        )
    )


def materialize_sampled_market(bundle: SampledMarketBundle) -> MarketContext:
    """Adapt a model-owned sampled bundle into the simulator's market context."""

    return MarketContext(prices=MARKET_PRICES_FRAME.normalize(market_prices_from_levels(bundle)))
