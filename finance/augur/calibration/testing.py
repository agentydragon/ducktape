"""Hermetic mock price clients for calibration tests.

``mock_price_clients`` assembles a ``dict[Platform, PriceClient]`` from per-platform
price maps, synthesizing :class:`Market` values directly (production reads parse
mirrored snapshots — see ``evidence_clients``; snapshot-parse fidelity is covered by
``test_evidence_clients``). An unknown market id raises :class:`MarketNotMirroredError`,
faithfully modeling the mirror-miss path calibration drops rows on.

A Kalshi ticker maps to either a bare ``float`` (a tight two-sided book at that
probability — the common case, exercising the mid path) or a :class:`KalshiRungQuote`
to model a real order book (wide spread, one-sided, stale/fractional last trade) for
the aggregation tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from finance.augur.calibration.evidence_clients import MarketNotMirroredError
from finance.augur.calibration.platform import Market, PriceClient
from finance.augur.calibration.quote import BookQuote, PoolQuote
from finance.evidence.markets import Platform


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


def _manifold_market(market_id: str, price: float) -> Market:
    return Market(
        id=market_id,
        url=f"https://manifold.markets/test/{market_id}",
        quote=PoolQuote(price=price),
        title=market_id,
        rules=f"Resolves per market {market_id}.",
    )


def _kalshi_market(market_id: str, spec: float | KalshiRungQuote) -> Market:
    # A bare float is a tight two-sided book at that probability (bid == ask == p, with depth and a
    # matching last trade), so it resolves to the mid p and stays the simple default for callers.
    quote = (
        KalshiRungQuote(bid=spec, ask=spec, bid_size=1.0, ask_size=1.0, last=spec, volume=1.0)
        if isinstance(spec, float | int)
        else spec
    )
    return Market(
        id=market_id,
        url=f"https://kalshi.test/{market_id}",
        quote=BookQuote(
            bid=quote.bid, ask=quote.ask, bid_size=quote.bid_size, ask_size=quote.ask_size, last_trade=quote.last
        ),
        volume=quote.volume,
        title=market_id,
        rules=f"Resolves per market {market_id}.",
    )


def _book_market(market_id: str, price: float) -> Market:
    return Market(
        id=market_id,
        url=f"https://test.example/{market_id}",
        quote=BookQuote(bid=price, ask=price, bid_size=None, ask_size=None, last_trade=price),
        title=market_id,
        rules=f"Resolves per market {market_id}.",
    )


class _StaticClient:
    """Minimal ``PriceClient`` over a fixed id -> Market map; unknown ids read as mirror misses."""

    def __init__(self, markets: Mapping[str, Market]) -> None:
        self._markets = markets

    async def get_market(self, market_id: str) -> Market:
        if market_id not in self._markets:
            raise MarketNotMirroredError(f"no mock market {market_id!r}")
        return self._markets[market_id]

    async def aclose(self) -> None:
        pass


def mock_price_clients(
    prices_by_platform: Mapping[Platform, Mapping[str, float | KalshiRungQuote]],
) -> dict[Platform, PriceClient]:
    """Build a ``dict[Platform, PriceClient]`` of hermetic static clients.

    Quote shapes match production parsing per platform: Manifold prices become
    ``PoolQuote``s, Kalshi entries (floats or :class:`KalshiRungQuote`) become
    ``BookQuote``s with depth, other platforms get a depthless tight book. Only
    Kalshi accepts :class:`KalshiRungQuote` values.
    """
    result: dict[Platform, PriceClient] = {}
    for platform, prices in prices_by_platform.items():
        if platform is Platform.KALSHI:
            markets = {market_id: _kalshi_market(market_id, spec) for market_id, spec in prices.items()}
        else:
            build = _manifold_market if platform is Platform.MANIFOLD else _book_market
            markets = {
                market_id: build(market_id, _require_float(platform, value)) for market_id, value in prices.items()
            }
        result[platform] = _StaticClient(markets)
    return result


def _require_float(platform: Platform, value: float | KalshiRungQuote) -> float:
    if not isinstance(value, float | int):
        raise TypeError(f"{platform} mock takes bare-float probabilities, not {type(value).__name__}")
    return value
