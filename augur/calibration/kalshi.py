"""Live Kalshi prices via their REST API.

Read-only market lookups against the Kalshi Trade API v2 (no auth needed for
public reads). Raw ``httpx`` — no official Python SDK exists. The API returns
``last_price_dollars`` on a 0-1 scale (already a probability).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from pydantic import BaseModel, ConfigDict

from augur.calibration.platform import Market
from augur.calibration.transient_retry import httpx_is_transient, with_retry

_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
_MARKET_URL_TEMPLATE = "https://kalshi.com/markets/{ticker}"


class _KalshiMarket(BaseModel):
    """The subset of Kalshi's `/markets/{ticker}` payload calibration needs.

    `last_price_dollars` is a stringified Decimal on a 0-1 scale (already a probability);
    `volume_fp` is all-time contracts traded (each Kalshi binary contract resolves $0-$1,
    so contract count is an upper bound on dollar volume but not directly comparable)."""

    model_config = ConfigDict(extra="ignore")

    last_price_dollars: float | None = None
    volume_fp: float | None = None
    # Title + verbatim resolution rules, surfaced live so the catalog needn't store/drift them.
    # `title` is the market's headline; `yes_sub_title` is the bucket/threshold leg (e.g.
    # "Above 3.0%"); `rules_primary` is the verbatim resolution criterion.
    title: str | None = None
    yes_sub_title: str | None = None
    rules_primary: str | None = None


class _KalshiResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: _KalshiMarket


class KalshiClient:
    """Live market lookups against Kalshi over a shared ``httpx.Client``.

    Recent market states are cached for ``cache_ttl_seconds``.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        cache_ttl_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._sleep = sleep
        self._cache: dict[str, tuple[Market, float]] = {}

    def get_market(self, market_id: str) -> Market:
        """One market's current state, served from the TTL cache when still fresh.

        ``market_id`` is the Kalshi ticker (e.g. ``"KXIPOOPENAI-26DEC01"``). Kalshi's API
        flaps `503` per-ticker, so the live read is retried with backoff before giving up.
        """
        now = self._clock()
        if (cached := self._cache.get(market_id)) is not None and now - cached[1] < self._cache_ttl_seconds:
            return cached[0]
        market = with_retry(
            lambda: self._fetch(market_id),
            what=f"kalshi market {market_id!r}",
            retry_on=httpx.HTTPError,
            is_transient=httpx_is_transient,
            sleep=self._sleep,
        )
        self._cache[market_id] = (market, now)
        return market

    def _fetch(self, market_id: str) -> Market:
        response = self._client.get(f"{_BASE_URL}/markets/{market_id}")
        response.raise_for_status()
        raw = _KalshiResponse.model_validate(response.json()).market
        # Kalshi's per-market title is the event headline; the leg's distinguishing clause lives in
        # yes_sub_title (e.g. "Above 3.0%"), so join them for a self-describing question.
        title = " — ".join(part for part in (raw.title, raw.yes_sub_title) if part) or None
        return Market(
            id=market_id,
            url=_MARKET_URL_TEMPLATE.format(ticker=market_id),
            probability=raw.last_price_dollars,
            volume=raw.volume_fp,
            volume_unit="contracts" if raw.volume_fp is not None else None,
            title=title,
            rules=raw.rules_primary,
        )

    def fetch_yes_probability(self, market_id: str) -> float:
        return self.get_market(market_id).require_probability()

    def close(self) -> None:
        self._client.close()
