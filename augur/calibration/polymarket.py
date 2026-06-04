"""Live Polymarket prices via the ``polymarket-client`` SDK.

Read-only market lookups using :class:`polymarket.PublicClient` (no auth
required). The SDK's ``Market.outcomes.yes.price`` (a ``Decimal``) is converted
to a plain ``float`` for the generic :class:`Market` probability.

Same TTL-cache + clock-seam pattern as :class:`ManifoldClient` so the live
calibration auto-refresh doesn't re-hit Polymarket per market per request.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal

from polymarket import PublicClient

from augur.calibration.platform import Market

_POLYMARKET_BASE_URL = "https://polymarket.com/event"


def _yes_price_to_probability(price: Decimal | None) -> float | None:
    if price is None:
        return None
    return float(price)


class PolymarketClient:
    """Live market lookups against Polymarket over a shared ``PublicClient``.

    Recent market states are cached for ``cache_ttl_seconds``.
    """

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sdk_client: PublicClient | None = None,
    ) -> None:
        self._sdk = sdk_client if sdk_client is not None else PublicClient()
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[Market, float]] = {}

    def get_market(self, market_id: str) -> Market:
        """One market's current state, served from the TTL cache when still fresh.

        ``market_id`` is the Polymarket condition_id passed to
        ``PublicClient.get_market(id=...)``.
        """
        now = self._clock()
        if (cached := self._cache.get(market_id)) is not None and now - cached[1] < self._cache_ttl_seconds:
            return cached[0]
        pm_market = self._sdk.get_market(id=market_id)
        probability = _yes_price_to_probability(
            pm_market.outcomes.yes.price if pm_market.outcomes is not None else None
        )
        slug = pm_market.slug or market_id
        # All-time traded volume in USD per the gamma `metrics.volume` field. The SDK's
        # Market.metrics is non-optional but each volume sub-field is.
        volume = float(pm_market.metrics.volume) if pm_market.metrics.volume is not None else None
        market = Market(
            id=market_id,
            url=f"{_POLYMARKET_BASE_URL}/{slug}",
            probability=probability,
            volume=volume,
            volume_unit="USD" if volume is not None else None,
            title=pm_market.question,
            rules=pm_market.description,
        )
        self._cache[market_id] = (market, now)
        return market

    def fetch_yes_probability(self, market_id: str) -> float:
        return self.get_market(market_id).require_probability()

    def close(self) -> None:
        self._sdk.__exit__(None, None, None)
