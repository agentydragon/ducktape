"""Per-magisterium grouping of level-series values, shared by the independent-provider family.

The non-PE level series partition into three disjoint magisteria by *what references them*
(see `augur/model/series.py`): asset-price (`sp500`, `crypto`), property-value (`home_value`),
and index (`inflation`, `rent`). Specs that map each level series to some value — the
independent exogenous provider and its sim/bench twin map each to a scalar model spec — all
range over that same magisterium-structured set. The structure lives here once as generic
groups so each consumer holds typed, magisterium-separated fields (mirroring the runtime
`SampledExogenousBundle`) rather than one flat per-kind bucket or a prefix-parsed
`dict[str, ValueT]`.

Field names are exactly the `LevelSeriesKind` values. Each magisterium projects to its *own*
typed-key view (`by_asset_price_key` / `by_property_value_key` / `by_index_series_key`); there
is deliberately no cross-magisterium merge into one `dict[LevelSeriesKey, ValueT]` — a consumer
that needs all three iterates them separately so the magisterium each series belongs to stays
visible at the call site.
"""

from __future__ import annotations

from pydantic import Field

from augur.model.schemas import FrozenModel
from augur.model.series import (
    AssetPriceKey,
    CryptoKey,
    CryptoSymbol,
    HomeValueKey,
    IndexSeriesKey,
    InflationKey,
    LocationId,
    PropertyValueKey,
    RentKey,
    SP500Key,
)


class AssetPriceGroups[ValueT](FrozenModel):
    """Asset-price magisterium values: the `sp500` singleton + `crypto` keyed by symbol."""

    sp500: ValueT | None = None
    crypto: dict[CryptoSymbol, ValueT] = Field(default_factory=dict)

    def by_asset_price_key(self) -> dict[AssetPriceKey, ValueT]:
        result: dict[AssetPriceKey, ValueT] = {}
        if self.sp500 is not None:
            result[SP500Key()] = self.sp500
        for symbol, value in self.crypto.items():
            result[CryptoKey(symbol=symbol)] = value
        return result


class PropertyValueGroups[ValueT](FrozenModel):
    """Property-value magisterium values: `home_value` keyed by location."""

    home_value: dict[LocationId, ValueT] = Field(default_factory=dict)

    def by_property_value_key(self) -> dict[PropertyValueKey, ValueT]:
        return {HomeValueKey(location_id=location_id): value for location_id, value in self.home_value.items()}


class IndexSeriesGroups[ValueT](FrozenModel):
    """Index magisterium values: the `inflation` singleton + `rent` keyed by location."""

    inflation: ValueT | None = None
    rent: dict[LocationId, ValueT] = Field(default_factory=dict)

    def by_index_series_key(self) -> dict[IndexSeriesKey, ValueT]:
        result: dict[IndexSeriesKey, ValueT] = {}
        if self.inflation is not None:
            result[InflationKey()] = self.inflation
        for location_id, value in self.rent.items():
            result[RentKey(location_id=location_id)] = value
        return result


class LevelSeriesMagisteria[ValueT](FrozenModel):
    """Level-series values separated into the three magisteria, mirroring `SampledExogenousBundle`.

    Each magisterium is its own typed sub-group (asset-price / property-value / index) rather
    than one flat per-kind bucket, so a cross-magisterium miswiring is unrepresentable.
    `extra="forbid"` (from `FrozenModel`) makes a stray top-level wire-id key such as
    `"crypto:btc"`, or a kind field placed in the wrong magisterium, fail at load instead of
    silently parsing — the desired fail-loud on pre-migration configs.

    There is intentionally no `by_level_key` flattening: a consumer that needs every level
    series reaches through the three magisterium fields (`asset_prices` / `property_values` /
    `index_series`) and their per-magisterium projections, keeping the magisterium boundary
    structural rather than collapsing it into one opaque keyspace.
    """

    asset_prices: AssetPriceGroups[ValueT] = Field(default_factory=AssetPriceGroups)
    property_values: PropertyValueGroups[ValueT] = Field(default_factory=PropertyValueGroups)
    index_series: IndexSeriesGroups[ValueT] = Field(default_factory=IndexSeriesGroups)
