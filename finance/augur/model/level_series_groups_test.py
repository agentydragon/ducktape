from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.level_series_groups import (
    AssetPriceGroups,
    IndexSeriesGroups,
    LevelSeriesGroups,
    PropertyValueGroups,
)
from finance.augur.model.series import SP500_SYMBOL, HomeValueKey, InflationKey, RentKey, SecurityKey


def test_asset_price_groups_project_by_symbol() -> None:
    groups = AssetPriceGroups[int].model_validate({"security": {"SPY": 2, "btc": 3, "eth": 4}})
    assert groups.by_asset_price_key() == {
        SecurityKey(symbol=SP500_SYMBOL): 2,
        SecurityKey(symbol="btc"): 3,
        SecurityKey(symbol="eth"): 4,
    }


def test_property_value_groups_project_by_location() -> None:
    groups = PropertyValueGroups[int].model_validate({"home_value": {"san_francisco_ca": 5}})
    assert groups.by_property_value_key() == {HomeValueKey(location_id="san_francisco_ca"): 5}


def test_index_series_groups_project_singleton_and_locations() -> None:
    groups = IndexSeriesGroups[int].model_validate({"inflation": 1, "rent": {"vallejo_ca": 6}})
    assert groups.by_index_series_key() == {InflationKey(): 1, RentKey(location_id="vallejo_ca"): 6}


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
        SecurityKey(symbol="btc"): 3,
    }
    assert roles.property_values.by_property_value_key() == {HomeValueKey(location_id="san_francisco_ca"): 5}
    assert roles.index_series.by_index_series_key() == {InflationKey(): 1, RentKey(location_id="vallejo_ca"): 6}


def test_extra_forbid_rejects_flat_kind_at_roles_top_level() -> None:
    # A pre-migration flat shape (a kind field at the top level rather than inside its
    # role sub-group) must fail loudly at load, not be silently accepted or dropped.
    with pytest.raises(ValidationError):
        LevelSeriesGroups[int].model_validate({"security": 2})


def test_extra_forbid_rejects_legacy_prefix_keys() -> None:
    # The point of the typed shape: an old-style wire-id key must fail loudly at load.
    with pytest.raises(ValidationError):
        AssetPriceGroups[int].model_validate({"security:btc": 1})


if __name__ == "__main__":
    pytest_bazel.main()
