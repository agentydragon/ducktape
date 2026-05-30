from __future__ import annotations

import pytest
import pytest_bazel

from augur.model.series import (
    CryptoKey,
    HomeValueKey,
    InflationKey,
    RentKey,
    SP500Key,
    parse_level_series_key,
    try_parse_level_series_key,
)


def test_level_series_key_round_trip_through_wire_id() -> None:
    for key in (
        InflationKey(),
        SP500Key(),
        HomeValueKey(location_id="san_francisco_ca"),
        RentKey(location_id="vallejo_ca"),
        CryptoKey(symbol="btc"),
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


if __name__ == "__main__":
    pytest_bazel.main()
