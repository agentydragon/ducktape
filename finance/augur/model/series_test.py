from __future__ import annotations

import pytest
import pytest_bazel

from finance.augur.model.series import (
    SP500_SYMBOL,
    HomeValueKey,
    InflationKey,
    LocationId,
    RentKey,
    SecurityKey,
    SecuritySymbol,
    parse_level_series_key,
    try_parse_level_series_key,
)

BTC = SecuritySymbol("btc")


def test_level_series_key_round_trip_through_wire_id() -> None:
    for key in (
        InflationKey(),
        SecurityKey(symbol=SP500_SYMBOL),
        HomeValueKey(location_id=LocationId("san_francisco_ca")),
        RentKey(location_id=LocationId("vallejo_ca")),
        SecurityKey(symbol=BTC),
    ):
        assert parse_level_series_key(key.wire_id) == key


def test_parse_level_series_key_rejects_unknown_wire_ids() -> None:
    for wire_id in ("", "unknown", "home_value", "private_equity:acme", "private_equity_regime_code:acme"):
        with pytest.raises(ValueError, match="unrecognized level-series wire id"):
            parse_level_series_key(wire_id)


def test_try_parse_level_series_key_returns_none_for_pe_wire_ids() -> None:
    assert try_parse_level_series_key("private_equity:acme") is None
    assert try_parse_level_series_key("private_equity_regime_code:acme") is None


if __name__ == "__main__":
    pytest_bazel.main()
