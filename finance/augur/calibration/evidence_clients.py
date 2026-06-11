"""Mirror-backed price reads: market quotes come from the evidence checkout.

The evidence scraper (`finance/scraper`) mirrors every rostered + catalog-referenced
market into the augur-evidence repo; the augur Deployment's git-sync sidecar keeps a
checkout at `AUGUR_EVIDENCE_DIR`. :class:`EvidenceMarketReader` implements
:class:`PriceClient` over that checkout, so calibration does no market-API network
I/O at request time: quote staleness is bounded by the scraper cadence, and the last
successfully-synced state is always available (upstream flaps cost freshness, never
rows). A market the mirror doesn't have raises :class:`MarketNotMirroredError`, which the
calibration run handles by dropping that row — the same operator experience as a
failed live fetch, and self-healing once the scraper picks up the new catalog entry.

Parse failures propagate: a snapshot that exists but doesn't parse is corrupt mirror
data (a bug), not a recoverable outage.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from finance.augur.calibration.platform import Market
from finance.augur.calibration.quote import BookQuote, PoolQuote
from finance.evidence import kalshi, manifold, polymarket
from finance.evidence.markets import Platform, market_json_path

_KALSHI_URL_TEMPLATE = "https://kalshi.com/markets/{ticker}"
_POLYMARKET_URL_TEMPLATE = "https://polymarket.com/event/{slug}"


class MarketNotMirroredError(Exception):
    """The checkout has no snapshot for this market (not yet scraped or not rostered)."""


def _parse_manifold(market_id: str, data: bytes) -> Market:
    record = manifold.load_market(data)
    return Market(
        id=record.id,
        url=record.url,
        # Manifold is a CPMM AMM: `probability` is the pool-implied fair price (a mid), with no
        # separate order book — a PoolQuote. MULTIPLE_CHOICE markets carry no whole-market price.
        quote=PoolQuote(price=record.probability) if record.probability is not None else None,
        volume=record.volume,
        # Manifold's brand symbol for mana is double-struck capital M (U+1D544); RUF001 flags
        # it as ambiguous with plain capital M, but the resemblance is intentional.
        volume_unit="𝕄",  # noqa: RUF001
        title=record.question,
        rules=record.text_description,
    )


def _book_side(price: float | None) -> float | None:
    """A real top-of-book YES price, or None for the untraded placeholder (a 0 bid / a 1 ask)."""
    return price if price is not None and 0.0 < price < 1.0 else None


def _positive(size: float | None) -> float | None:
    return size or None


def _parse_kalshi(market_id: str, data: bytes) -> Market:
    record = kalshi.load_market(data)
    # Kalshi's per-market title is the event headline; the leg's distinguishing clause lives in
    # yes_sub_title (e.g. "Above 3.0%"), so join them for a self-describing question.
    title = " — ".join(part for part in (record.title, record.yes_sub_title) if part) or None
    return Market(
        id=market_id,
        url=_KALSHI_URL_TEMPLATE.format(ticker=market_id),
        # Kalshi reports an absent book side as a 0 bid / a 1 ask (the 1-cent quote on the
        # opposite outcome); map those placeholders to None so they don't read as a real quote.
        quote=BookQuote(
            bid=_book_side(record.yes_bid_dollars),
            ask=_book_side(record.yes_ask_dollars),
            bid_size=_positive(record.yes_bid_size_fp),
            ask_size=_positive(record.yes_ask_size_fp),
            last_trade=record.last_price_dollars,
        ),
        volume=record.volume_fp,
        volume_unit="contracts" if record.volume_fp is not None else None,
        title=title,
        rules=record.rules_primary,
    )


def _parse_polymarket(market_id: str, data: bytes) -> Market:
    record = polymarket.load_market(data)
    volume = record.volume_num
    return Market(
        id=market_id,
        url=_POLYMARKET_URL_TEMPLATE.format(slug=record.slug or market_id),
        # Gamma carries the top-of-book quote but no resting size; depth is therefore unknown.
        quote=BookQuote(
            bid=record.best_bid, ask=record.best_ask, bid_size=None, ask_size=None, last_trade=record.last_trade_price
        ),
        volume=volume,
        volume_unit="USD" if volume is not None else None,
        title=record.question,
        rules=record.description,
    )


_PARSERS = {Platform.MANIFOLD: _parse_manifold, Platform.KALSHI: _parse_kalshi, Platform.POLYMARKET: _parse_polymarket}


@dataclass(frozen=True)
class EvidenceMarketReader:
    """`PriceClient` over one platform's mirrored snapshots in an evidence checkout."""

    platform: Platform
    evidence_dir: Path

    async def get_market(self, market_id: str) -> Market:
        path = market_json_path(self.evidence_dir, self.platform, market_id)
        if not path.exists():
            raise MarketNotMirroredError(f"no mirrored snapshot at {path}")
        return _PARSERS[self.platform](market_id, path.read_bytes())

    async def aclose(self) -> None:
        """No-op: the reader holds no connections."""
