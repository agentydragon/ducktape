from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.model.level_series_groups import (
    AssetPriceGroups,
    IndexSeriesGroups,
    LevelSeriesMagisteria,
    PropertyValueGroups,
)
from augur.model.series import CryptoKey, HomeValueKey, InflationKey, RentKey, SP500Key


def test_asset_price_groups_project_singleton_and_symbols() -> None:
    groups = AssetPriceGroups[int].model_validate({"sp500": 2, "crypto": {"btc": 3, "eth": 4}})
    assert groups.by_asset_price_key() == {SP500Key(): 2, CryptoKey(symbol="btc"): 3, CryptoKey(symbol="eth"): 4}


def test_asset_price_groups_omit_absent_singleton() -> None:
    # Absent singleton ⇒ no key at all (the series is unmodeled), not a key with a
    # None/zero value — this distinction is why the singleton field is `ValueT | None`.
    assert AssetPriceGroups[int].model_validate({"crypto": {"btc": 3}}).by_asset_price_key() == {
        CryptoKey(symbol="btc"): 3
    }


def test_property_value_groups_project_by_location() -> None:
    groups = PropertyValueGroups[int].model_validate({"home_value": {"san_francisco_ca": 5}})
    assert groups.by_property_value_key() == {HomeValueKey(location_id="san_francisco_ca"): 5}


def test_index_series_groups_project_singleton_and_locations() -> None:
    groups = IndexSeriesGroups[int].model_validate({"inflation": 1, "rent": {"vallejo_ca": 6}})
    assert groups.by_index_series_key() == {InflationKey(): 1, RentKey(location_id="vallejo_ca"): 6}


def test_magisteria_keep_each_series_in_its_own_group() -> None:
    # The three magisteria stay separate; each projects only to its own typed-key view,
    # and there is deliberately no cross-magisterium merge into one keyspace.
    magisteria = LevelSeriesMagisteria[int].model_validate(
        {
            "asset_prices": {"sp500": 2, "crypto": {"btc": 3}},
            "property_values": {"home_value": {"san_francisco_ca": 5}},
            "index_series": {"inflation": 1, "rent": {"vallejo_ca": 6}},
        }
    )
    assert magisteria.asset_prices.by_asset_price_key() == {SP500Key(): 2, CryptoKey(symbol="btc"): 3}
    assert magisteria.property_values.by_property_value_key() == {HomeValueKey(location_id="san_francisco_ca"): 5}
    assert magisteria.index_series.by_index_series_key() == {InflationKey(): 1, RentKey(location_id="vallejo_ca"): 6}


def test_extra_forbid_rejects_flat_kind_at_magisteria_top_level() -> None:
    # A pre-migration flat shape (a kind field at the top level rather than inside its
    # magisterium sub-group) must fail loudly at load, not be silently accepted or dropped.
    with pytest.raises(ValidationError):
        LevelSeriesMagisteria[int].model_validate({"sp500": 2})


def test_extra_forbid_rejects_legacy_prefix_keys() -> None:
    # The point of the typed shape: an old-style wire-id key must fail loudly at load.
    with pytest.raises(ValidationError):
        AssetPriceGroups[int].model_validate({"crypto:btc": 1})


if __name__ == "__main__":
    pytest_bazel.main()
