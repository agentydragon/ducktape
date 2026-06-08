"""Diagnostic: run the REAL quote -> probability -> ladder-distribution pipeline against live
Kalshi data for the configured CPI-YoY threshold ladder, and contrast it with the old
last-trade-based survival curve.

Throwaway investigation tool (hence `x/`) for the CPI market-line spikiness. It uses the actual
``KalshiClient`` parser, :func:`implied_probability` / :func:`quote_confidence`, and the real
``_fit_ladder_curve`` + ``_threshold_ladder_buckets`` so what it prints is exactly what
calibration produces. Run via ``bb run`` with the sandbox disabled (Kalshi host is off the sandbox
allowlist):

    bb run //finance/augur/calibration/x:inspect_cpi_quotes
"""

from __future__ import annotations

import asyncio
from datetime import date

from finance.augur.calibration.calibration import _fit_ladder_curve, _threshold_ladder_buckets
from finance.augur.calibration.catalog import ThresholdLadderFamily, ThresholdLadderMember
from finance.augur.calibration.kalshi import KalshiClient
from finance.augur.calibration.platform import Direction, Market, Platform
from finance.augur.calibration.quote import BookQuote, implied_probability, quote_confidence

_TICKERS = [f"KXCPIYOY-26JUL-T{whole}.{tenth}" for whole in (3, 4) for tenth in range(10)] + ["KXCPIYOY-26JUL-T5.0"]
_THRESHOLDS = [whole * 0.01 + tenth * 0.001 for whole in (3, 4) for tenth in range(10)] + [0.05]


def _fmt(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "-"


def _buckets(curve: list[float] | None) -> list[float] | None:
    if curve is None:
        return None
    family = ThresholdLadderFamily(
        family_id="cpi",
        question="cpi",
        platform=Platform.KALSHI,
        series="inflation",
        value_kind="inflation_yoy",
        direction=Direction.ABOVE,
        at_date=date(2026, 7, 31),
        thresholds=[ThresholdLadderMember(market_id=f"T{t}", threshold=t) for t in _THRESHOLDS],
    )
    return [
        round(bucket.p_market, 3) for bucket in _threshold_ladder_buckets(family, thresholds=_THRESHOLDS, curve=curve)
    ]


def _tight_book_market(value: float) -> Market:
    return Market(
        id="x", url="u", quote=BookQuote(bid=value, ask=value, bid_size=1.0, ask_size=1.0, last_trade=value), volume=1.0
    )


async def main() -> None:
    client = KalshiClient()
    try:
        markets: list[Market | None] = [await client.get_market(ticker) for ticker in _TICKERS]
    finally:
        await client.aclose()

    print(f"{'rung':>5} {'bid':>6} {'ask':>6} {'last':>6} {'implied':>8} {'weight':>10}")
    last_markets: list[Market | None] = []
    for ticker, market in zip(_TICKERS, markets, strict=True):
        assert market is not None
        quote = market.quote
        assert isinstance(quote, BookQuote)
        prob = implied_probability(quote, volume=market.volume)
        weight = quote_confidence(quote, volume=market.volume)
        last_markets.append(_tight_book_market(quote.last_trade) if quote.last_trade is not None else None)
        print(
            f"{ticker.split('-T')[-1]:>5} {_fmt(quote.bid):>6} {_fmt(quote.ask):>6} {_fmt(quote.last_trade):>6} "
            f"{_fmt(prob):>8} {weight:>10.1f}"
        )

    print(
        "\nNEW buckets (quote-mid, weighted isotonic):",
        _buckets(_fit_ladder_curve(_THRESHOLDS, markets, increasing=False)),
    )
    print(
        "OLD buckets (last-trade, uniform):         ",
        _buckets(_fit_ladder_curve(_THRESHOLDS, last_markets, increasing=False)),
    )


if __name__ == "__main__":
    asyncio.run(main())
