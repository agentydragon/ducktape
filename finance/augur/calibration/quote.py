"""Turn a prediction market's live quote into a probability estimate and a confidence weight.

Markets do not emit a probability; they emit a *quote* — an order book (a bid and ask, each with
resting size) on order-book platforms (Kalshi, Polymarket), or a single pool-implied price on an
AMM (Manifold). The last trade is a poor signal: on a thin book it can be a stale or sub-$1
fractional fill far from the live book. This module is the principled conversion layer:

- :func:`implied_probability` — a single point estimate per market. For a two-sided book it is the
  size-weighted micro-price (Stoikov), which corrects for order-book imbalance and degenerates to
  the plain midpoint when sizes are absent or equal. An AMM pool price is already a fair mid. A
  one-sided / empty book yields the volume-backed last trade only as a last resort, else ``None``
  (no observation) — never a fabricated ``0.5`` mid or a fake ``0`` from an untraded contract.
- :func:`quote_confidence` — an inverse-variance weight (tight, deep books pull hard; wide or thin
  ones barely count) used to weight the isotonic fit when aggregating a ladder of markets into a
  distribution. See ``calibration._monotone_probabilities``.

A binary contract's bid/ask is a no-arbitrage band ``bid <= p <= ask``; the spread is the
market's own statement of how uncertain it is.
"""

from __future__ import annotations

from dataclasses import dataclass

# Confidence tuning knobs (not load-bearing semantics — they only set relative weights within one
# ladder's isotonic fit). A perfectly tight book would otherwise get infinite weight, so the spread
# is floored at one cent; a one-sided/last-only rung is a weak signal with a small fixed weight.
_SPREAD_FLOOR = 0.01
_ONE_SIDED_WEIGHT = 0.05


@dataclass(frozen=True)
class BookQuote:
    """Top-of-book snapshot for an order-book binary market; prices in [0,1] YES-probability units.

    Either side may be ``None`` (one-sided or empty book); ``*_size`` is the resting contract/share
    quantity at that price (``None`` when the platform does not surface depth, e.g. Polymarket's
    gamma response). ``last_trade`` may be present even with an empty book, but is only ever a
    fallback — see :func:`implied_probability`.
    """

    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    last_trade: float | None

    @property
    def is_two_sided(self) -> bool:
        """A genuine book on both sides — not the untraded placeholder (a 0 bid / a 1 ask, which
        Kalshi reports for a contract with only a 1-cent quote on the opposite outcome)."""
        return self.bid is not None and self.ask is not None and self.bid > 0.0 and self.ask < 1.0


@dataclass(frozen=True)
class PoolQuote:
    """An AMM CPMM pool-implied price (Manifold) — already a fair mid, no separate order book."""

    price: float


Quote = BookQuote | PoolQuote | None


def implied_probability(quote: Quote, *, volume: float | None) -> float | None:
    """One YES-probability point estimate, or ``None`` when the quote carries no information.

    ``volume`` is the market's all-time traded volume, used only to gate the one-sided last-trade
    fallback (an untraded contract's ``last`` is meaningless).
    """
    match quote:
        case PoolQuote(price=price):
            return price
        case BookQuote() as book:
            if book.is_two_sided:
                assert book.bid is not None
                assert book.ask is not None
                if book.bid_size and book.ask_size:
                    # Stoikov micro-price: weight each side by the *opposite* side's size, so heavy
                    # resting size on one side pulls the estimate toward the other quote.
                    return (book.bid * book.ask_size + book.ask * book.bid_size) / (book.bid_size + book.ask_size)
                return (book.bid + book.ask) / 2
            return book.last_trade if (book.last_trade and volume) else None
        case None:
            return None


def quote_confidence(quote: Quote, *, volume: float | None) -> float:
    """Inverse-variance weight for aggregating this market into a ladder fit.

    A two-sided book's variance grows with the squared spread and shrinks with resting depth (or
    all-time volume when depth is unknown). A pool price is treated as an exact, depth-weighted
    observation; a one-sided/last-only rung gets a small fixed weight; no observation gets zero.
    """
    match quote:
        case PoolQuote():
            return _depth_proxy(None, None, volume)
        case BookQuote() as book:
            if book.is_two_sided:
                assert book.bid is not None
                assert book.ask is not None
                spread = max(book.ask - book.bid, _SPREAD_FLOOR)
                return _depth_proxy(book.bid_size, book.ask_size, volume) / (spread * spread)
            return _ONE_SIDED_WEIGHT
        case None:
            return 0.0


def _depth_proxy(bid_size: float | None, ask_size: float | None, volume: float | None) -> float:
    """How much size backs the quote: the thinner top-of-book side when known, else all-time
    volume, else a neutral 1.0. Floored at 1.0 so a quote never gets zero confidence purely for
    lacking a depth figure."""
    if bid_size and ask_size:
        return max(1.0, min(bid_size, ask_size))
    return max(1.0, volume or 0.0)
