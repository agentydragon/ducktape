"""Hermetic mock clients for calibration tests.

``mock_manifold_client`` and ``mock_kalshi_client`` build real client classes over
``httpx.MockTransport`` so they exercise the client's actual caching/parsing path.
``mock_price_clients`` is a convenience that assembles a ``dict[Platform, PriceClient]``
from per-platform price maps.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

import httpx

from finance.augur.calibration.kalshi import KalshiClient
from finance.augur.calibration.manifold import ManifoldClient
from finance.augur.calibration.platform import Market, Platform, PriceClient


def mock_manifold_client(
    prices: Mapping[str, float], *, clock: Callable[[], float] = time.monotonic, cache_ttl_seconds: float = 120.0
) -> ManifoldClient:
    """A ``ManifoldClient`` whose market reads resolve from `prices` keyed by Manifold id."""

    def handler(request: httpx.Request) -> httpx.Response:
        market_id = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "id": market_id,
                "url": f"https://manifold.markets/test/{market_id}",
                "probability": prices[market_id],
                # Title/criterion are fetched live in production; the hermetic mock echoes the id.
                "question": market_id,
                "textDescription": f"Resolves per market {market_id}.",
            },
        )

    return ManifoldClient(
        clock=clock,
        cache_ttl_seconds=cache_ttl_seconds,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def mock_kalshi_client(
    prices: Mapping[str, float], *, clock: Callable[[], float] = time.monotonic, cache_ttl_seconds: float = 120.0
) -> KalshiClient:
    """A ``KalshiClient`` whose market reads resolve from `prices` (0-1 scale) keyed by ticker."""

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "market": {
                    "last_price_dollars": str(prices[ticker]),
                    "ticker": ticker,
                    "title": ticker,
                    "rules_primary": f"Resolves per market {ticker}.",
                }
            },
        )

    return KalshiClient(
        clock=clock,
        cache_ttl_seconds=cache_ttl_seconds,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class _StaticClient:
    """Minimal ``PriceClient`` backed by a fixed price map."""

    def __init__(self, prices: Mapping[str, float]) -> None:
        self._prices = prices

    async def get_market(self, market_id: str) -> Market:
        return Market(
            id=market_id,
            url=f"https://test.example/{market_id}",
            probability=self._prices[market_id],
            title=market_id,
            rules=f"Resolves per market {market_id}.",
        )

    async def aclose(self) -> None:
        pass


def mock_price_clients(
    prices_by_platform: Mapping[Platform, Mapping[str, float]],
    *,
    clock: Callable[[], float] = time.monotonic,
    cache_ttl_seconds: float = 120.0,
) -> dict[Platform, PriceClient]:
    """Build a ``dict[Platform, PriceClient]`` with hermetic mock clients.

    Manifold and Kalshi use their real client classes (exercising parsing/caching).
    Polymarket (and any future platform without a specialised mock) gets a
    ``_StaticClient`` that returns ``Market`` directly.
    """
    result: dict[Platform, PriceClient] = {}
    for platform, prices in prices_by_platform.items():
        if platform == Platform.MANIFOLD:
            result[platform] = mock_manifold_client(prices, clock=clock, cache_ttl_seconds=cache_ttl_seconds)
        elif platform == Platform.KALSHI:
            result[platform] = mock_kalshi_client(prices, clock=clock, cache_ttl_seconds=cache_ttl_seconds)
        else:
            result[platform] = _StaticClient(prices)
    return result
