"""Hermetic mock clients for calibration tests.

``mock_manifold_client`` and ``mock_kalshi_client`` build real client classes over
``httpx.MockTransport`` so they exercise the client's actual caching/parsing path.
``mock_price_clients`` is a convenience that assembles a ``dict[Platform, PriceClient]``
from per-platform price maps.

A Kalshi ticker maps to either a bare ``float`` (a tight two-sided book at that probability — the
common case, exercising the mid path) or a :class:`KalshiRungQuote` to model a real order book
(wide spread, one-sided, stale/fractional last trade) for the aggregation tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from finance.augur.calibration.kalshi import KalshiClient
from finance.augur.calibration.manifold import ManifoldClient
from finance.augur.calibration.platform import Market, Platform, PriceClient
from finance.augur.calibration.quote import BookQuote


@dataclass(frozen=True)
class KalshiRungQuote:
    """A Kalshi market's order book for a mock response: top-of-book YES bid/ask (0-1) with resting
    sizes, the last trade, and all-time volume. Any field may be omitted to model a missing side."""

    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last: float | None = None
    volume: float | None = None


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


def _kalshi_market_json(ticker: str, spec: float | KalshiRungQuote) -> dict[str, object]:
    # A bare float is a tight two-sided book at that probability (bid == ask == p, with depth and a
    # matching last trade), so it resolves to the mid p and stays the simple default for callers.
    quote = (
        KalshiRungQuote(bid=spec, ask=spec, bid_size=1.0, ask_size=1.0, last=spec, volume=1.0)
        if isinstance(spec, float | int)
        else spec
    )
    market: dict[str, object] = {"ticker": ticker, "title": ticker, "rules_primary": f"Resolves per market {ticker}."}
    fields = {
        "yes_bid_dollars": quote.bid,
        "yes_ask_dollars": quote.ask,
        "yes_bid_size_fp": quote.bid_size,
        "yes_ask_size_fp": quote.ask_size,
        "last_price_dollars": quote.last,
        "volume_fp": quote.volume,
    }
    market.update({key: str(value) for key, value in fields.items() if value is not None})
    return {"market": market}


def mock_kalshi_client(
    prices: Mapping[str, float | KalshiRungQuote],
    *,
    clock: Callable[[], float] = time.monotonic,
    cache_ttl_seconds: float = 120.0,
) -> KalshiClient:
    """A ``KalshiClient`` whose market reads resolve from `prices` keyed by ticker; each value is a
    bare probability (tight book) or a :class:`KalshiRungQuote` order book."""

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(200, json=_kalshi_market_json(ticker, prices[ticker]))

    return KalshiClient(
        clock=clock,
        cache_ttl_seconds=cache_ttl_seconds,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class _StaticClient:
    """Minimal ``PriceClient`` returning a tight two-sided book at each id's probability."""

    def __init__(self, prices: Mapping[str, float]) -> None:
        self._prices = prices

    async def get_market(self, market_id: str) -> Market:
        price = self._prices[market_id]
        return Market(
            id=market_id,
            url=f"https://test.example/{market_id}",
            quote=BookQuote(bid=price, ask=price, bid_size=None, ask_size=None, last_trade=price),
            title=market_id,
            rules=f"Resolves per market {market_id}.",
        )

    async def aclose(self) -> None:
        pass


def mock_price_clients(
    prices_by_platform: Mapping[Platform, Mapping[str, float | KalshiRungQuote]],
    *,
    clock: Callable[[], float] = time.monotonic,
    cache_ttl_seconds: float = 120.0,
) -> dict[Platform, PriceClient]:
    """Build a ``dict[Platform, PriceClient]`` with hermetic mock clients.

    Manifold and Kalshi use their real client classes (exercising parsing/caching).
    Polymarket (and any future platform without a specialised mock) gets a
    ``_StaticClient`` that returns ``Market`` directly. Only Kalshi accepts
    :class:`KalshiRungQuote` values; the others require bare floats.
    """
    result: dict[Platform, PriceClient] = {}
    for platform, prices in prices_by_platform.items():
        if platform == Platform.KALSHI:
            result[platform] = mock_kalshi_client(prices, clock=clock, cache_ttl_seconds=cache_ttl_seconds)
        else:
            floats = {market_id: _require_float(platform, value) for market_id, value in prices.items()}
            if platform == Platform.MANIFOLD:
                result[platform] = mock_manifold_client(floats, clock=clock, cache_ttl_seconds=cache_ttl_seconds)
            else:
                result[platform] = _StaticClient(floats)
    return result


def _require_float(platform: Platform, value: float | KalshiRungQuote) -> float:
    if not isinstance(value, float | int):
        raise TypeError(f"{platform} mock takes bare-float probabilities, not {type(value).__name__}")
    return value
