from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from finance.augur.model.series import (
    SP500_SYMBOL,
    AssetPriceKey,
    HomeValueKey,
    IndexSeriesKey,
    InflationKey,
    PropertyValueKey,
    RentKey,
    SecurityKey,
    parse_level_series_key,
    try_parse_level_series_key,
)

_INDEX_ADAPTER: TypeAdapter[IndexSeriesKey] = TypeAdapter(IndexSeriesKey)
_ASSET_PRICE_ADAPTER: TypeAdapter[AssetPriceKey] = TypeAdapter(AssetPriceKey)
_PROPERTY_VALUE_ADAPTER: TypeAdapter[PropertyValueKey] = TypeAdapter(PropertyValueKey)


def test_level_series_key_round_trip_through_wire_id() -> None:
    for key in (
        InflationKey(),
        SecurityKey(symbol=SP500_SYMBOL),
        HomeValueKey(location_id="san_francisco_ca"),
        RentKey(location_id="vallejo_ca"),
        SecurityKey(symbol="btc"),
    ):
        assert parse_level_series_key(key.wire_id) == key


def test_level_series_key_kind_serializes_as_readable_string_and_round_trips() -> None:
    # The StrEnum discriminator is the reason the key is typed-but-readable: a
    # serialized key carries a readable string `kind` (not an opaque int) and
    # reconstructs from that plain dict. Config, API wire, and trained-artifact
    # serialization in later phases all rely on this.
    inflation_dump = InflationKey().model_dump(mode="json")
    assert isinstance(inflation_dump["kind"], str)
    assert InflationKey.model_validate(inflation_dump) == InflationKey()

    security_dump = SecurityKey(symbol="btc").model_dump(mode="json")
    assert isinstance(security_dump["kind"], str)
    assert SecurityKey.model_validate(security_dump) == SecurityKey(symbol="btc")


def test_parse_level_series_key_rejects_unknown_wire_ids() -> None:
    for wire_id in ("", "unknown", "home_value", "private_equity:acme", "private_equity_regime_code:acme"):
        with pytest.raises(ValueError, match="unrecognized level-series wire id"):
            parse_level_series_key(wire_id)


def test_try_parse_level_series_key_returns_none_for_pe_wire_ids() -> None:
    assert try_parse_level_series_key("private_equity:acme") is None
    assert try_parse_level_series_key("private_equity_regime_code:acme") is None


def test_roles_unions_accept_their_members_and_reject_others() -> None:
    # The role split is enforced statically by mypy on reference fields;
    # the discriminated unions also reject foreign members at runtime. A series
    # only escalates amounts if it's an index; only prices a lot if asset-price;
    # only values a property if home-value.
    assert _INDEX_ADAPTER.validate_python(InflationKey().model_dump()) == InflationKey()
    assert _INDEX_ADAPTER.validate_python(RentKey(location_id="sf").model_dump()) == RentKey(location_id="sf")
    assert _ASSET_PRICE_ADAPTER.validate_python(SecurityKey(symbol="btc").model_dump()) == SecurityKey(symbol="btc")
    assert _PROPERTY_VALUE_ADAPTER.validate_python(HomeValueKey(location_id="sf").model_dump()) == HomeValueKey(
        location_id="sf"
    )

    # Cross-role values are rejected: a security (asset price) is not an index,
    # rent (index) is not an asset price, and a security is not a property value.
    with pytest.raises(ValidationError):
        _INDEX_ADAPTER.validate_python(SecurityKey(symbol=SP500_SYMBOL).model_dump())
    with pytest.raises(ValidationError):
        _ASSET_PRICE_ADAPTER.validate_python(RentKey(location_id="sf").model_dump())
    with pytest.raises(ValidationError):
        _PROPERTY_VALUE_ADAPTER.validate_python(SecurityKey(symbol="btc").model_dump())


if __name__ == "__main__":
    pytest_bazel.main()
