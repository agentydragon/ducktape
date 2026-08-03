from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from finance.augur.model.series import (
    AssetPriceKey,
    CryptoKey,
    DiscountRateKey,
    HomeValueKey,
    IndexSeriesKey,
    InflationKey,
    MuniRatioKey,
    NominalYieldKey,
    PropertyValueKey,
    RentKey,
    SP500Key,
    parse_level_series_key,
    try_parse_level_series_key,
)

_INDEX_ADAPTER: TypeAdapter[IndexSeriesKey] = TypeAdapter(IndexSeriesKey)
_ASSET_PRICE_ADAPTER: TypeAdapter[AssetPriceKey] = TypeAdapter(AssetPriceKey)
_PROPERTY_VALUE_ADAPTER: TypeAdapter[PropertyValueKey] = TypeAdapter(PropertyValueKey)
_DISCOUNT_RATE_ADAPTER: TypeAdapter[DiscountRateKey] = TypeAdapter(DiscountRateKey)


def test_level_series_key_round_trip_through_wire_id() -> None:
    for key in (
        InflationKey(),
        SP500Key(),
        HomeValueKey(location_id="san_francisco_ca"),
        RentKey(location_id="vallejo_ca"),
        CryptoKey(symbol="btc"),
        NominalYieldKey(tenor_months=120),
        MuniRatioKey(tenor_months=360),
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

    crypto_dump = CryptoKey(symbol="btc").model_dump(mode="json")
    assert isinstance(crypto_dump["kind"], str)
    assert CryptoKey.model_validate(crypto_dump) == CryptoKey(symbol="btc")


def test_parse_level_series_key_rejects_unknown_wire_ids() -> None:
    for wire_id in ("", "unknown", "home_value", "private_equity:acme", "private_equity_regime_code:acme"):
        with pytest.raises(ValueError, match="unrecognized level-series wire id"):
            parse_level_series_key(wire_id)


def test_try_parse_level_series_key_returns_none_for_pe_wire_ids() -> None:
    assert try_parse_level_series_key("private_equity:acme") is None
    assert try_parse_level_series_key("private_equity_regime_code:acme") is None


def test_magisteria_unions_accept_their_members_and_reject_others() -> None:
    # The magisterium split is enforced statically by mypy on reference fields;
    # the discriminated unions also reject foreign members at runtime. A series
    # only escalates amounts if it's an index; only prices a lot if asset-price;
    # only values a property if home-value.
    assert _INDEX_ADAPTER.validate_python(InflationKey().model_dump()) == InflationKey()
    assert _INDEX_ADAPTER.validate_python(RentKey(location_id="sf").model_dump()) == RentKey(location_id="sf")
    assert _ASSET_PRICE_ADAPTER.validate_python(SP500Key().model_dump()) == SP500Key()
    assert _ASSET_PRICE_ADAPTER.validate_python(CryptoKey(symbol="btc").model_dump()) == CryptoKey(symbol="btc")
    assert _PROPERTY_VALUE_ADAPTER.validate_python(HomeValueKey(location_id="sf").model_dump()) == HomeValueKey(
        location_id="sf"
    )

    # Cross-magisterium values are rejected: sp500 (asset price) is not an index,
    # rent (index) is not an asset price, crypto (asset price) is not a property value.
    with pytest.raises(ValidationError):
        _INDEX_ADAPTER.validate_python(SP500Key().model_dump())
    with pytest.raises(ValidationError):
        _ASSET_PRICE_ADAPTER.validate_python(RentKey(location_id="sf").model_dump())
    with pytest.raises(ValidationError):
        _PROPERTY_VALUE_ADAPTER.validate_python(CryptoKey(symbol="btc").model_dump())


def test_discount_rate_keys_are_their_own_magisterium() -> None:
    # A rate is neither a price nor an index: nothing is valued by holding one and nothing is
    # escalated by one, so wiring a bond's discount curve to inflation (or a lot's price to a
    # yield) must not typecheck OR validate.
    assert _DISCOUNT_RATE_ADAPTER.validate_python(NominalYieldKey(tenor_months=120).model_dump()) == NominalYieldKey(
        tenor_months=120
    )
    assert _DISCOUNT_RATE_ADAPTER.validate_python(MuniRatioKey(tenor_months=120).model_dump()) == MuniRatioKey(
        tenor_months=120
    )

    with pytest.raises(ValidationError):
        _DISCOUNT_RATE_ADAPTER.validate_python(InflationKey().model_dump())
    with pytest.raises(ValidationError):
        _INDEX_ADAPTER.validate_python(NominalYieldKey(tenor_months=120).model_dump())
    with pytest.raises(ValidationError):
        _ASSET_PRICE_ADAPTER.validate_python(NominalYieldKey(tenor_months=120).model_dump())


def test_rate_wire_ids_require_an_integer_tenor() -> None:
    # The tenor is part of the identity, so a malformed one must fail loudly rather than
    # silently parsing as some default tenor.
    assert try_parse_level_series_key("nominal_yield:abc") is None
    assert try_parse_level_series_key("muni_ratio:") is None
    assert try_parse_level_series_key("nominal_yield:120") == NominalYieldKey(tenor_months=120)


if __name__ == "__main__":
    pytest_bazel.main()
