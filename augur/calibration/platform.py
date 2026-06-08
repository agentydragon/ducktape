"""Platform-agnostic types for prediction-market price fetching.

The calibration core (``calibration.py``) consumes only the :class:`Market`
dataclass and the :class:`PriceClient` protocol — it never touches a
platform-specific API directly. Each platform client (``manifold.py``,
``polymarket.py``, ``kalshi.py``) implements :class:`PriceClient` and
translates its native response into a :class:`Market`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Platform(StrEnum):
    MANIFOLD = "manifold"
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class Direction(StrEnum):
    """Which side of a threshold resolves a market YES.

    `ABOVE` is `value >= threshold`; `BELOW` is `value < threshold`. The two are exact
    complements, so a YES/NO threshold and a half-open `[low, high)` bucket family tile the
    line without gaps or overlap. Lives here (the neutral platform-types module) so both the
    catalog schema and the resolvers can reference it without a dependency cycle.
    """

    ABOVE = "above"
    BELOW = "below"


@dataclass(frozen=True)
class Market:
    """Platform-agnostic snapshot of a prediction market's current state.

    `volume` is the platform's all-time traded-volume figure in its native unit, identified by
    `volume_unit` (e.g. ``"USD"`` for Polymarket, ``"M$"`` for Manifold mana, ``"contracts"``
    for Kalshi - each Kalshi binary contract resolves to $0-$1 so contract count is a
    bounded-above proxy for dollar volume but isn't directly comparable). `None` when the
    platform's response carried no volume figure.
    """

    id: str
    url: str
    # TODO(naming): markets don't expose a "probability" — they expose a last trade / bid / ask, and
    # `probability` here is really the last-trade price read as an implied probability (e.g. Kalshi
    # `last_price_dollars`, Manifold `probability`, Polymarket yes.price). The actual probability comes
    # from the separate price->probability smoothing step. Rename this (and `require_probability`) to
    # something honest like `last_trade_price` / `implied_probability` so it doesn't read as a
    # calibrated probability. Cross-cutting across the platform clients + calibration; see augur/TODO.md.
    probability: float | None
    volume: float | None = None
    volume_unit: str | None = None
    # The market's current title/question and verbatim resolution rules, fetched LIVE alongside the
    # price so they can't drift from the platform. `None` when the response carried none. The
    # catalog no longer stores these per market — they are populated from this live snapshot.
    title: str | None = None
    rules: str | None = None

    def require_probability(self) -> float:
        if self.probability is None:
            raise ValueError(f"Market {self.id!r} returned no YES probability")
        return self.probability


class PriceClient(Protocol):
    async def get_market(self, market_id: str) -> Market: ...
    async def aclose(self) -> None: ...
