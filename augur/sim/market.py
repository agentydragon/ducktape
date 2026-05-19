"""Market bundle — exogenous per-(asset, rollout, month) price paths.

The market is the only source of rollout divergence at spike 1: agent
decisions don't feed back into the market, and within a rollout every
agent reads the same price path. The bundle is materialized at sim
start into a long-form polars frame keyed by
`(rollout_index, month_index, asset_id)` with one column
`price_per_unit_usd`. Subsequent step calls index into it by month.

Two path-generator kinds are supported at spike 1:

- **DeterministicPath**: a fixed list of monthly prices that the
  same value applies across every rollout. Useful for tests and for
  the bench scenario's deterministic-comparison case.
- **GeometricBrownianPath**: per-rollout GBM samples driven by
  `numpy.random.default_rng(asset_seed)`. Each asset has its own
  seeded generator so results are reproducible.

Real production market models are out of scope for spike 1; this is
just enough infrastructure to prove the rollout-divergence
plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

import numpy as np
import polars as pl
from pydantic import BaseModel, Field

MARKET_PRICES_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "asset_id": pl.Utf8(),
    "price_per_unit_usd": pl.Float64(),
}


class DeterministicPath(BaseModel):
    """A fixed per-month price curve shared across rollouts. The
    `prices_usd` list runs from month 0 through `horizon_months`
    inclusive; the engine asserts the length matches the scenario
    horizon at materialization time."""

    kind: Literal["deterministic"] = "deterministic"
    asset_id: str
    prices_usd: list[float]


class GeometricBrownianPath(BaseModel):
    """Per-rollout GBM-sampled price path. `initial_price_usd` is
    month-0 price; subsequent months apply `exp(N(mu, sigma))` to
    the previous month's price. Sampling uses
    `numpy.random.default_rng(rng_seed)` so the same seed yields
    the same paths across runs."""

    kind: Literal["gbm"] = "gbm"
    asset_id: str
    initial_price_usd: float
    monthly_log_return_mu: float = 0.0
    monthly_log_return_sigma: float = 0.0
    rng_seed: int = 0


MarketPathSpec = Annotated[DeterministicPath | GeometricBrownianPath, Field(discriminator="kind")]


class MarketBundle(BaseModel):
    """The full set of per-asset price-path specifications. Empty
    bundle is valid (no assets traded in the scenario)."""

    paths: list[MarketPathSpec] = Field(default_factory=list)


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


def materialize_market(bundle: MarketBundle, *, rollout_count: int, horizon_months: int) -> MarketContext:
    """Realize every path spec into a long-form polars frame and
    bundle it as a `MarketContext`. The output covers months 0
    through `horizon_months` inclusive (so length `horizon_months
    + 1` per (rollout, asset)). An empty bundle yields an empty
    frame with the correct schema."""
    if not bundle.paths:
        return MarketContext(prices=pl.DataFrame(schema=MARKET_PRICES_SCHEMA))
    blocks = [_materialize_path(p, rollout_count=rollout_count, horizon_months=horizon_months) for p in bundle.paths]
    return MarketContext(prices=pl.concat(blocks).select(list(MARKET_PRICES_SCHEMA.keys())))


def _materialize_path(path: MarketPathSpec, *, rollout_count: int, horizon_months: int) -> pl.DataFrame:
    if isinstance(path, DeterministicPath):
        return _materialize_deterministic(path, rollout_count=rollout_count, horizon_months=horizon_months)
    return _materialize_gbm(path, rollout_count=rollout_count, horizon_months=horizon_months)


def _materialize_deterministic(path: DeterministicPath, *, rollout_count: int, horizon_months: int) -> pl.DataFrame:
    expected = horizon_months + 1
    if len(path.prices_usd) != expected:
        msg = (
            f"DeterministicPath for {path.asset_id} has {len(path.prices_usd)} prices; "
            f"need {expected} (month 0..{horizon_months} inclusive)"
        )
        raise ValueError(msg)
    months = pl.DataFrame(
        {"month_index": list(range(expected)), "price_per_unit_usd": path.prices_usd},
        schema={"month_index": pl.Int64(), "price_per_unit_usd": pl.Float64()},
    ).with_columns(pl.lit(path.asset_id, dtype=pl.Utf8()).alias("asset_id"))
    rollouts = pl.DataFrame({"rollout_index": list(range(rollout_count))}, schema={"rollout_index": pl.Int64()})
    return rollouts.join(months, how="cross").select(list(MARKET_PRICES_SCHEMA.keys()))


def _materialize_gbm(path: GeometricBrownianPath, *, rollout_count: int, horizon_months: int) -> pl.DataFrame:
    """Sample (rollout_count, horizon_months) log-returns and
    accumulate them into per-month prices. `numpy.random.default_rng`
    is the canonical generator — seeding it with `path.rng_seed`
    yields identical paths across runs."""
    rng = np.random.default_rng(path.rng_seed)
    # Shape (rollouts, horizon): one log-return per (rollout, transition).
    log_returns = rng.normal(
        loc=path.monthly_log_return_mu, scale=path.monthly_log_return_sigma, size=(rollout_count, horizon_months)
    )
    cumulative = np.cumsum(log_returns, axis=1)  # cumulative log return at end of month m+1
    prices = np.empty((rollout_count, horizon_months + 1), dtype=np.float64)
    prices[:, 0] = path.initial_price_usd
    prices[:, 1:] = path.initial_price_usd * np.exp(cumulative)
    rollout_idx = np.repeat(np.arange(rollout_count, dtype=np.int64), horizon_months + 1)
    month_idx = np.tile(np.arange(horizon_months + 1, dtype=np.int64), rollout_count)
    flat_prices = prices.reshape(-1)
    return pl.DataFrame(
        {
            "rollout_index": rollout_idx,
            "month_index": month_idx,
            "asset_id": [path.asset_id] * (rollout_count * (horizon_months + 1)),
            "price_per_unit_usd": flat_prices,
        },
        schema=MARKET_PRICES_SCHEMA,
    )
