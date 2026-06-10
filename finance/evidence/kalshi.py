"""Typed records for mirrored Kalshi data (`markets/kalshi/<ticker>/market.json`).

The mirror stores the Trade API v2 `/markets/{ticker}` response as served: a
`{"market": {...}}` wrapper whose prices are stringified decimals on a 0-1 scale
(`*_dollars`) and sizes fixed-point strings (`*_fp`). Quote interpretation (placeholder
bids/asks, micro-price) is the consumer's concern — augur's calibration reader maps
these records onto its quote types.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class KalshiMarket(BaseModel):
    """The subset of Kalshi's `/markets/{ticker}` payload shared consumers read.

    `yes_bid_dollars`/`yes_ask_dollars` are the top-of-book YES quote;
    `yes_bid_size_fp`/`yes_ask_size_fp` the resting contract counts there.
    `last_price_dollars` is the last trade. `volume_fp` is all-time contracts traded.
    `title` is the market headline; `yes_sub_title` the bucket/threshold leg (e.g.
    "Above 3.0%"); `rules_primary` the verbatim resolution criterion.
    """

    model_config = ConfigDict(extra="ignore")

    ticker: str | None = None
    yes_bid_dollars: float | None = None
    yes_ask_dollars: float | None = None
    yes_bid_size_fp: float | None = None
    yes_ask_size_fp: float | None = None
    last_price_dollars: float | None = None
    volume_fp: float | None = None
    title: str | None = None
    yes_sub_title: str | None = None
    rules_primary: str | None = None
    status: str | None = None
    result: str | None = None


class KalshiResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: KalshiMarket


def load_market(data: bytes) -> KalshiMarket:
    """Parse a stored `market.json` (the wrapped `/markets/{ticker}` response body)."""
    return KalshiResponse.model_validate_json(data).market
