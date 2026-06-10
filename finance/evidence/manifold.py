"""Typed records + loaders for mirrored Manifold data (`markets/manifold/<id>/`).

The mirror stores Manifold's responses as served (camelCase JSON; `market.json` plus
`bets.jsonl`/`comments.jsonl` for deep entries — see `finance.evidence.markets`).
These models parse that raw form into typed records and polars frames; `prob_at`
implements the verified price-reconstruction rule (probAfter of the last bet
at-or-before the cutoff — loom/plans/market_harvest.md).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _ManifoldRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)


class ManifoldMarket(_ManifoldRecord):
    """The subset of `/v0/market/{id}` shared consumers read.

    Timestamps are Manifold's raw epoch-milliseconds ints. `probability` is the
    CPMM pool price — present on BINARY markets only (MULTIPLE_CHOICE markets carry
    per-answer pools instead).
    """

    id: str
    url: str
    question: str | None = None
    outcome_type: str | None = None
    probability: float | None = None
    volume: float | None = None
    unique_bettor_count: int | None = None
    created_time: int | None = None
    close_time: int | None = None
    is_resolved: bool | None = None
    resolution: str | None = None
    resolution_time: int | None = None
    last_updated_time: int | None = None
    text_description: str | None = None


class ManifoldBet(_ManifoldRecord):
    """One row of `bets.jsonl`. `answer_id` is set on multiple-choice markets only."""

    id: str
    created_time: int
    prob_before: float | None = None
    prob_after: float | None = None
    amount: float | None = None
    outcome: str | None = None
    is_redemption: bool = False
    answer_id: str | None = None
    user_id: str | None = None


class ManifoldComment(_ManifoldRecord):
    """One row of `comments.jsonl`. `content` is Manifold's raw TipTap JSON tree."""

    id: str
    created_time: int
    user_id: str | None = None
    user_name: str | None = None
    content: dict[str, Any] | None = None


def load_market(data: bytes) -> ManifoldMarket:
    return ManifoldMarket.model_validate_json(data)


def _ms_to_datetime(column: str) -> pl.Expr:
    # The explicit final cast pins the column dtype (epoch-ms source precision)
    # regardless of what unit from_epoch/replace_time_zone produce internally.
    return pl.from_epoch(pl.col(column), time_unit="ms").dt.replace_time_zone("UTC").cast(pl.Datetime("ms", "UTC"))


_BETS_SCHEMA = {
    "id": pl.String,
    "created_time": pl.Int64,
    "prob_before": pl.Float64,
    "prob_after": pl.Float64,
    "amount": pl.Float64,
    "outcome": pl.String,
    "is_redemption": pl.Boolean,
    "answer_id": pl.String,
    "user_id": pl.String,
}

_COMMENTS_SCHEMA = {"id": pl.String, "created_time": pl.Int64, "user_id": pl.String, "user_name": pl.String}


def bets_frame(data: bytes) -> pl.DataFrame:
    """Parse `bets.jsonl` bytes into a frame (file order preserved; `created_time` is UTC)."""
    bets = [ManifoldBet.model_validate_json(line) for line in data.splitlines() if line.strip()]
    frame = pl.DataFrame({field: [getattr(bet, field) for bet in bets] for field in _BETS_SCHEMA}, schema=_BETS_SCHEMA)
    return frame.with_columns(_ms_to_datetime("created_time"))


def comments_frame(data: bytes) -> pl.DataFrame:
    """Parse `comments.jsonl` bytes into a frame (TipTap bodies stay in the raw file)."""
    comments = [ManifoldComment.model_validate_json(line) for line in data.splitlines() if line.strip()]
    frame = pl.DataFrame(
        {field: [getattr(comment, field) for comment in comments] for field in _COMMENTS_SCHEMA},
        schema=_COMMENTS_SCHEMA,
    )
    return frame.with_columns(_ms_to_datetime("created_time"))


def prob_at(bets: pl.DataFrame, when: datetime) -> float | None:
    """The market's YES probability implied at `when` (tz-aware).

    `probAfter` of the last bet with `created_time <= when` — the reconstruction rule
    verified in loom/plans/market_harvest.md. Rows without `prob_after` are ignored;
    returns None when no bet precedes `when`. Whole-market semantics are binary-only:
    for a multiple-choice market, pre-filter the frame on `answer_id` to get one
    answer's path.
    """
    eligible = bets.filter((pl.col("created_time") <= when) & pl.col("prob_after").is_not_null())
    if eligible.is_empty():
        return None
    # Stable sort keeps file order (the API's tie order) for equal timestamps.
    return eligible.sort("created_time", maintain_order=True)[-1, "prob_after"]
