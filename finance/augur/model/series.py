"""Typed identifiers for exogenous level series and PE protocol codes.

The augur sim<->model boundary used to encode the *kind* of a series in a
magic prefix on its string id (`"home_value:..."`, `"security:..."`,
`"private_equity_regime_code:..."`, etc.) and have every consumer dispatch on
`series_id.startswith(...)`. That dispatch is now typed: every non-PE level
series is identified by a `LevelSeriesKey` variant (a Pydantic discriminated
union with a `StrEnum` `kind` discriminator), and the PE protocol bundle
lives in its own typed `PrivateEquityBundle` indexed by `IssuerId`.

The wire string format is preserved for serialization and human-readable
logs/IDs; producers and consumers obtain it via `LevelSeriesKey.wire_id` and
recover the typed key via `parse_level_series_key`. Outside of those two
boundary functions, no augur code should be matching prefixes.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Annotated, Literal, NewType

from pydantic import Field

from finance.augur.model.schemas import FrozenModel

IssuerId = NewType("IssuerId", str)
LocationId = NewType("LocationId", str)
SecuritySymbol = NewType("SecuritySymbol", str)


class LevelSeriesKind(StrEnum):
    """Discriminator for `LevelSeriesKey` variants.

    `StrEnum` (not `IntEnum`) so the discriminator renders as a human-readable
    `kind: security` wherever a `LevelSeriesKey` is Pydantic-serialized (config,
    API wire, trained artifacts). The values double as the per-kind field names
    within each role group (`AssetPriceGroups.security`, `IndexSeriesGroups.rent`)
    and on the sampled-levels bundle, so `key.kind` is both the discriminator and
    the in-role attribute name.
    """

    INFLATION = "inflation"
    SECURITY = "security"
    HOME_VALUE = "home_value"
    RENT = "rent"


class _LevelKeyBase(FrozenModel):
    @property
    def wire_id(self) -> str:
        raise NotImplementedError

    @property
    def subid(self) -> str | None:
        """This key's value in its frame's sub-id column, or `None` for a singleton kind.

        A key knows its own sub-id; the per-kind table in `exogenous.py` only has to know
        which *column* that sub-id lands in and how to rebuild a key from one.
        """

        return None


class InflationKey(_LevelKeyBase):
    kind: Literal[LevelSeriesKind.INFLATION] = LevelSeriesKind.INFLATION

    @property
    def wire_id(self) -> str:
        return "inflation"


class SecurityKey(_LevelKeyBase):
    """A tradable security's unit-price series, keyed by its symbol.

    One kind covers every traded thing — a broad-market index proxy, an individual
    stock, a crypto ticker. They differ in the PARAMETERS fitted for them, not in
    what anything downstream does with them: a lot is a quantity, a cost basis and a
    price path in every case, disposed and taxed by the same machinery. Where a real
    per-instrument difference exists it is per-symbol data (the quantity quantum in
    `sim/fixed_point.py`) or an owner-declared attribute (the liquidity bucket, taken
    from the holding's `security_kind`) — never a type, since a type would have to
    answer why two crypto symbols share one and two equities share another.
    """

    kind: Literal[LevelSeriesKind.SECURITY] = LevelSeriesKind.SECURITY
    symbol: SecuritySymbol

    @property
    def wire_id(self) -> str:
        return f"security:{self.symbol}"

    @property
    def subid(self) -> str | None:
        return str(self.symbol)


SP500_SYMBOL = SecuritySymbol("SPY")
"""Symbol of the broad-market equity series.

`SPY`, not `SP500`: the fitted factor's evidence anchor is SPY's adjusted close, so the
symbol names the instrument the data actually comes from. A holding tracking the index by
some other vehicle still points its `value_series` here — the series is the market, the
holding's own `symbol` is what the owner holds.
"""


SP500_KEY = SecurityKey(symbol=SP500_SYMBOL)
"""The broad-market equity series. Named because evidence loading, fitting, and the
portfolio's index-proxy holdings all have to agree on which symbol means "the market"."""


class HomeValueKey(_LevelKeyBase):
    kind: Literal[LevelSeriesKind.HOME_VALUE] = LevelSeriesKind.HOME_VALUE
    location_id: LocationId

    @property
    def wire_id(self) -> str:
        return f"home_value:{self.location_id}"

    @property
    def subid(self) -> str | None:
        return str(self.location_id)


class RentKey(_LevelKeyBase):
    kind: Literal[LevelSeriesKind.RENT] = LevelSeriesKind.RENT
    location_id: LocationId

    @property
    def wire_id(self) -> str:
        return f"rent:{self.location_id}"

    @property
    def subid(self) -> str | None:
        return str(self.location_id)


# Series roles: non-PE level series partition by WHAT REFERENCES them. The split is
# load-bearing typing — a reference field annotated with one role cannot be
# wired to a series from another (a lot priced by inflation, rent escalated by
# a stock price, …), so those cross-wirings are mypy errors. `LevelSeriesKey` is the sum,
# used only where a helper genuinely ranges over all non-PE level series.
type AssetPriceKey = Annotated[SecurityKey, Field(discriminator="kind")]
"""Prices a holding/lot: the security's own symbol (PE marks are off in their own bundle)."""

type PropertyValueKey = Annotated[HomeValueKey, Field(discriminator="kind")]
"""Values a property at sale: the location's home-value series."""

type IndexSeriesKey = Annotated[InflationKey | RentKey, Field(discriminator="kind")]
"""Escalates a recurring amount: CPI inflation or a location's rent series."""

type LevelSeriesKey = Annotated[InflationKey | SecurityKey | HomeValueKey | RentKey, Field(discriminator="kind")]


class PrivateEquityRegimeCode(IntEnum):
    """Sim-facing issuer operating modes.

    Keep this enum to states that change holder-visible liquidity or accounting behavior.
    Model-internal latent states such as business distress should stay in the model and
    affect the emitted protocol channels instead of being exposed directly to the simulator.
    Liquidity suspension is represented by the separate `liquidity_blocked` protocol channel.
    """

    PRIVATE_OPERATING = 1
    PUBLIC_MARKET = 2
    ACQUIRED = 3
    COLLAPSED = 4


class PrivateEquityEventKindCode(IntEnum):
    NONE = 0
    TENDER = 1
    ADMIN_MARK_UPDATE = 2
    PUBLIC_MARKET_OPEN = 3
    ACQUISITION_CASHOUT = 4
    LEGAL_IMPAIRMENT = 5
    FORCED_RECOVERY = 6
    COLLAPSE = 7


def parse_level_series_key(wire_id: str) -> LevelSeriesKey:
    """Recover a typed `LevelSeriesKey` from its wire form.

    The only place in augur that decodes the prefix-encoded series-id string.
    Raises `ValueError` for unrecognized wire ids (including private-equity
    wire ids — PE is not a level series and is carried in the typed PE bundle).
    """

    if wire_id == "inflation":
        return InflationKey()
    prefix, sep, suffix = wire_id.partition(":")
    if not sep:
        raise ValueError(f"unrecognized level-series wire id {wire_id!r}")
    match prefix:
        case "home_value":
            return HomeValueKey(location_id=LocationId(suffix))
        case "rent":
            return RentKey(location_id=LocationId(suffix))
        case "security":
            return SecurityKey(symbol=SecuritySymbol(suffix))
    raise ValueError(f"unrecognized level-series wire id {wire_id!r}")


def try_parse_level_series_key(wire_id: str) -> LevelSeriesKey | None:
    """Return a typed key or `None` if the wire id is not a known level series.

    Useful for filters that need to skip private-equity series (which are
    carried in the typed PE bundle and have no `LevelSeriesKey` representation).
    """

    try:
        return parse_level_series_key(wire_id)
    except ValueError:
        return None
