"""Typed records for mirrored Polymarket data (`markets/polymarket/<id>/market.json`).

The mirror stores the gamma API response as served: a one-element JSON list for
condition-id queries (`/markets?condition_ids=0x...`), a bare object for numeric-id
fetches (`/markets/{id}`). `load_market` accepts both. Prices are 0-1 YES-probability
floats; gamma surfaces top-of-book without resting size.
"""

from __future__ import annotations

import json

from more_itertools import one
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class GammaMarket(BaseModel):
    """The subset of a gamma market object shared consumers read."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    id: str
    condition_id: str | None = None
    slug: str | None = None
    question: str | None = None
    description: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    volume_num: float | None = None
    closed: bool | None = None


def load_market(data: bytes) -> GammaMarket:
    """Parse a stored `market.json` (bare gamma object, or the raw one-element list)."""
    payload = json.loads(data)
    if isinstance(payload, list):
        payload = one(payload)
    return GammaMarket.model_validate(payload)
