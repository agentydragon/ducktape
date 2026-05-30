"""Per-kind grouping of level-series values, shared by config surfaces.

Several config surfaces map each non-PE level series to some value: the
independent provider maps each to a scalar model spec, state-space conditioning
maps each to observed points, the VECM provider maps each to a latest float.
They all range over the same set of level-series kinds (inflation, sp500,
crypto, home_value, rent), so the per-kind field layout lives here once as a
generic base instead of being re-declared — and re-prefix-parsed — at each
surface.

The field names are exactly the `LevelSeriesKind` values, so a populated
`LevelSeriesGroups` is the typed, prefix-free replacement for the old
`dict[str, ValueT]` keyed by `"crypto:btc"`-style wire ids. `by_level_key`
projects the groups into the canonical `dict[LevelSeriesKey, ValueT]` the
runtime consumes.
"""

from __future__ import annotations

from pydantic import Field

from augur.model.schemas import FrozenModel
from augur.model.series import (
    CryptoKey,
    CryptoSymbol,
    HomeValueKey,
    InflationKey,
    LevelSeriesKey,
    LocationId,
    RentKey,
    SP500Key,
)


class LevelSeriesGroups[ValueT](FrozenModel):
    """Level-series values grouped by kind; singletons scalar, others keyed by sub-id.

    Singleton kinds (`inflation`, `sp500`) hold a single value or `None` when the
    series is not modeled; `crypto`/`home_value`/`rent` hold a value per sub-id
    (symbol / location). `extra="forbid"` (from `FrozenModel`) makes a stray
    top-level wire-id key such as `"crypto:btc"` fail at load instead of silently
    parsing — the desired fail-loud on pre-migration configs.
    """

    inflation: ValueT | None = None
    sp500: ValueT | None = None
    crypto: dict[CryptoSymbol, ValueT] = Field(default_factory=dict)
    home_value: dict[LocationId, ValueT] = Field(default_factory=dict)
    rent: dict[LocationId, ValueT] = Field(default_factory=dict)

    def by_level_key(self) -> dict[LevelSeriesKey, ValueT]:
        """Project the per-kind groups into the canonical typed-key map.

        An absent singleton contributes no entry (absent means "not modeled", not
        a key with an empty value); crypto/home_value/rent each contribute one
        entry per sub-id.
        """

        result: dict[LevelSeriesKey, ValueT] = {}
        if self.inflation is not None:
            result[InflationKey()] = self.inflation
        if self.sp500 is not None:
            result[SP500Key()] = self.sp500
        for symbol, value in self.crypto.items():
            result[CryptoKey(symbol=symbol)] = value
        for location_id, value in self.home_value.items():
            result[HomeValueKey(location_id=location_id)] = value
        for location_id, value in self.rent.items():
            result[RentKey(location_id=location_id)] = value
        return result
