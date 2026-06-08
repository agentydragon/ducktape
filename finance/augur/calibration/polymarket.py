"""Live Polymarket prices via the ``polymarket-client`` SDK.

Read-only market lookups using :class:`polymarket.PublicClient` (no auth
required). The SDK's ``Market.outcomes.yes.price`` (a ``Decimal``) is converted
to a plain ``float`` for the generic :class:`Market` probability.

Same TTL-cache + clock-seam pattern as :class:`ManifoldClient` so the live
calibration auto-refresh doesn't re-hit Polymarket per market per request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

from polymarket import PublicClient

# `TimeoutError` / `TransportError` are aliased to avoid shadowing the builtins of the same
# name; these are the SDK's wait-timeout / transport failures, distinct from
# `builtins.TimeoutError` and `httpx.TransportError`.
from polymarket.errors import (
    RateLimitError,
    RequestRejectedError,
    TimeoutError as PolymarketTimeoutError,
    TransportError as PolymarketTransportError,
)

from finance.augur.calibration.platform import Market
from finance.augur.calibration.transient_retry import with_retry_async

_POLYMARKET_BASE_URL = "https://polymarket.com/event"


def _polymarket_is_transient(exc: BaseException) -> bool:
    """A Polymarket SDK failure worth retrying: a 5xx server rejection, a rate-limit, or a
    transport/wait-timeout error. Input-validation and unexpected-shape errors are not."""
    if isinstance(exc, RequestRejectedError):
        return exc.status >= 500
    return isinstance(exc, RateLimitError | PolymarketTransportError | PolymarketTimeoutError)


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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        sdk_client: PublicClient | None = None,
    ) -> None:
        self._sdk = sdk_client if sdk_client is not None else PublicClient()
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._sleep = sleep
        self._cache: dict[str, tuple[Market, float]] = {}

    async def get_market(self, market_id: str) -> Market:
        """One market's current state, served from the TTL cache when still fresh.

        ``market_id`` is the Polymarket condition_id passed to
        ``PublicClient.get_market(id=...)``. The Polymarket SDK is synchronous, so the live
        read runs in a worker thread; a transient 5xx/rate-limit/timeout is retried with
        backoff before propagating.
        """
        now = self._clock()
        if (cached := self._cache.get(market_id)) is not None and now - cached[1] < self._cache_ttl_seconds:
            return cached[0]
        market = await with_retry_async(
            lambda: asyncio.to_thread(self._fetch, market_id),
            what=f"polymarket market {market_id!r}",
            is_transient=_polymarket_is_transient,
            sleep=self._sleep,
        )
        self._cache[market_id] = (market, now)
        return market

    def _fetch(self, market_id: str) -> Market:
        pm_market = self._sdk.get_market(id=market_id)
        probability = _yes_price_to_probability(
            pm_market.outcomes.yes.price if pm_market.outcomes is not None else None
        )
        slug = pm_market.slug or market_id
        # All-time traded volume in USD per the gamma `metrics.volume` field. The SDK's
        # Market.metrics is non-optional but each volume sub-field is.
        volume = float(pm_market.metrics.volume) if pm_market.metrics.volume is not None else None
        return Market(
            id=market_id,
            url=f"{_POLYMARKET_BASE_URL}/{slug}",
            probability=probability,
            volume=volume,
            volume_unit="USD" if volume is not None else None,
            title=pm_market.question,
            rules=pm_market.description,
        )

    async def fetch_yes_probability(self, market_id: str) -> float:
        return (await self.get_market(market_id)).require_probability()

    async def aclose(self) -> None:
        await asyncio.to_thread(self._sdk.__exit__, None, None, None)
