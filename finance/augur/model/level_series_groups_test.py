from __future__ import annotations

import pytest_bazel

from finance.augur.model.level_series_groups import LevelSeriesGroups
from finance.augur.model.series import (
    SP500_SYMBOL,
    HomeValueKey,
    InflationKey,
    LocationId,
    RentKey,
    SecurityKey,
    SecuritySymbol,
)


def test_roles_keep_each_series_in_its_own_group() -> None:
    # The roles stay separate; each projects only to its own typed-key view,
    # and there is deliberately no cross-role merge into one keyspace.
    roles = LevelSeriesGroups[int].model_validate(
        {
            "asset_prices": {"security": {"SPY": 2, "btc": 3}},
            "property_values": {"home_value": {"san_francisco_ca": 5}},
            "index_series": {"inflation": 1, "rent": {"vallejo_ca": 6}},
        }
    )
    assert roles.asset_prices.by_asset_price_key() == {
        SecurityKey(symbol=SP500_SYMBOL): 2,
        SecurityKey(symbol=SecuritySymbol("btc")): 3,
    }
    assert roles.property_values.by_property_value_key() == {
        HomeValueKey(location_id=LocationId("san_francisco_ca")): 5
    }
    assert roles.index_series.by_index_series_key() == {
        InflationKey(): 1,
        RentKey(location_id=LocationId("vallejo_ca")): 6,
    }


if __name__ == "__main__":
    pytest_bazel.main()
