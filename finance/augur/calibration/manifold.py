"""Live Manifold prices: the current YES probability for a binary market.

Read-only Manifold v0 market endpoint (no key required; ``MANIFOLD_API_KEY`` is
honored for rate limits). Calibration prices ALWAYS come from live Manifold, so a
fetch that fails or returns no probability raises -- there is no catalog-stored
fallback to fall through to.

:meth:`ManifoldClient.get_market` is the primitive read: a single market state served
through a short TTL cache of recent pulls, so the live calibration auto-refresh doesn't
re-hit Manifold per market on every request. :meth:`ManifoldClient.fetch_yes_probability`
wraps it, returning the market's YES probability (or raising when it carries none).
Consumers use the concrete :class:`ManifoldClient`; tests inject a ``MockTransport``-backed
``httpx.Client`` (see ``augur.calibration.testing``) so they stay hermetic.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from finance.augur.calibration.platform import Market
from finance.augur.calibration.transient_retry import httpx_is_transient, with_retry_async

_MARKET_ENDPOINT = "https://api.manifold.markets/v0/market/"
_USER_AGENT = "augur-pm-calibration/1.0"


class _ManifoldResponse(BaseModel):
    """The subset of the Manifold v0 market payload calibration needs.

    Manifold serves camelCase JSON; `alias_generator=to_camel` maps it onto these snake_case
    fields, so any future multi-word field (e.g. `total_liquidity` <- `totalLiquidity`) parses
    without a manual rename pass."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    id: str
    url: str
    probability: float | None = None
    # All-time traded volume in mana (Manifold's play currency); Manifold returns this on
    # every market response so it's the natural "is this market thick" indicator.
    volume: float | None = None
    # Title + resolution prose, surfaced live so the catalog needn't store (and drift from) them.
    question: str | None = None
    text_description: str | None = None


def _headers() -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if key := os.environ.get("MANIFOLD_API_KEY"):
        headers["Authorization"] = f"Key {key}"
    return headers


class ManifoldClient:
    """Live market lookups against Manifold over a shared ``httpx.Client``.

    Recent market states are cached for ``cache_ttl_seconds`` so the live calibration
    auto-refresh (which re-scores the whole catalog on every input change) doesn't re-hit
    Manifold per market per request.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        cache_ttl_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client if client is not None else httpx.AsyncClient(headers=_headers(), timeout=timeout)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._sleep = sleep
        # market id -> (last fetched market state, monotonic timestamp of that fetch).
        self._cache: dict[str, tuple[Market, float]] = {}

    async def get_market(self, market_id: str) -> Market:
        """One market's current state, served from the TTL cache when still fresh.

        A transient 5xx/timeout is retried with backoff before propagating.
        """
        now = self._clock()
        if (cached := self._cache.get(market_id)) is not None and now - cached[1] < self._cache_ttl_seconds:
            return cached[0]
        market = await with_retry_async(
            lambda: self._fetch(market_id),
            what=f"manifold market {market_id!r}",
            is_transient=httpx_is_transient,
            sleep=self._sleep,
        )
        self._cache[market_id] = (market, now)
        return market

    async def _fetch(self, market_id: str) -> Market:
        response = await self._client.get(f"{_MARKET_ENDPOINT}{market_id}")
        response.raise_for_status()
        raw = _ManifoldResponse.model_validate(response.json())
        # Manifold's brand symbol for mana is double-struck capital M (U+1D544); RUF001 flags
        # it as ambiguous with plain capital M, but the resemblance is intentional.
        return Market(
            id=raw.id,
            url=raw.url,
            probability=raw.probability,
            volume=raw.volume,
            volume_unit="𝕄",  # noqa: RUF001
            title=raw.question,
            rules=raw.text_description,
        )

    async def fetch_yes_probability(self, market_id: str) -> float:
        """Current YES probability for one binary market; raises if it carries none."""
        return (await self.get_market(market_id)).require_probability()

    async def aclose(self) -> None:
        await self._client.aclose()
