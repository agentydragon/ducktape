"""Typed loader for mirrored Manifold market snapshots.

The mirror stores Manifold's responses as served (camelCase JSON). This loader
parses `market.json` for current calibration; deep bet and comment history remains
in the evidence mirror for the planned market harvest (loom/plans/market_harvest.md).
"""

from __future__ import annotations

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


def load_market(data: bytes) -> ManifoldMarket:
    return ManifoldMarket.model_validate_json(data)
