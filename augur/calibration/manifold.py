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

import os
import time
from collections.abc import Callable

import httpx
from pydantic import BaseModel, ConfigDict

from augur.api.casing import decamelize_json

_MARKET_ENDPOINT = "https://api.manifold.markets/v0/market/"
_USER_AGENT = "augur-pm-calibration/1.0"


class ManifoldMarket(BaseModel):
    """The subset of the Manifold v0 market payload calibration needs (snake-cased)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    # Canonical market page (`https://manifold.markets/<creator>/<slug>`); the catalog stores
    # only a slug, so the link text/href the frontend renders comes from the live payload.
    url: str
    probability: float | None = None

    def require_probability(self) -> float:
        """This market's YES probability; raises when the payload carries none."""
        if self.probability is None:
            raise ValueError(f"Manifold market {self.id!r} returned no YES probability")
        return self.probability


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
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client if client is not None else httpx.Client(headers=_headers(), timeout=timeout)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        # market id -> (last fetched market state, monotonic timestamp of that fetch).
        self._cache: dict[str, tuple[ManifoldMarket, float]] = {}

    def get_market(self, market_id: str) -> ManifoldMarket:
        """One market's current state, served from the TTL cache when still fresh."""
        now = self._clock()
        if (cached := self._cache.get(market_id)) is not None and now - cached[1] < self._cache_ttl_seconds:
            return cached[0]
        response = self._client.get(f"{_MARKET_ENDPOINT}{market_id}")
        response.raise_for_status()
        market = ManifoldMarket.model_validate(decamelize_json(response.json()))
        self._cache[market_id] = (market, now)
        return market

    def fetch_yes_probability(self, market_id: str) -> float:
        """Current YES probability for one binary market; raises if it carries none."""
        return self.get_market(market_id).require_probability()

    def close(self) -> None:
        self._client.close()
