from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from finance.augur.model.series import CryptoKey, HomeValueKey, InflationKey, IssuerId, LocationId, RentKey, SP500Key
from finance.augur.model.state_space_factor import FactorKey, PrivateEquityMarkKey, parse_factor_key

_ADAPTER: TypeAdapter[FactorKey] = TypeAdapter(FactorKey)


def test_parse_factor_key_decodes_level_series() -> None:
    assert parse_factor_key("inflation") == InflationKey()
    assert parse_factor_key("sp500") == SP500Key()
    assert parse_factor_key("crypto:btc") == CryptoKey(symbol="btc")
    assert parse_factor_key("home_value:san_francisco_ca") == HomeValueKey(location_id=LocationId("san_francisco_ca"))
    assert parse_factor_key("rent:vallejo_ca") == RentKey(location_id=LocationId("vallejo_ca"))


def test_parse_factor_key_decodes_private_equity_mark() -> None:
    assert parse_factor_key("private_equity:openai") == PrivateEquityMarkKey(issuer_id=IssuerId("openai"))


def test_wire_id_round_trips_through_parse() -> None:
    keys: list[FactorKey] = [
        InflationKey(),
        SP500Key(),
        CryptoKey(symbol="btc"),
        HomeValueKey(location_id=LocationId("san_francisco_ca")),
        RentKey(location_id=LocationId("vallejo_ca")),
        PrivateEquityMarkKey(issuer_id=IssuerId("openai")),
    ]
    for key in keys:
        assert parse_factor_key(key.wire_id) == key


def test_discriminated_union_validates_each_variant() -> None:
    # The structural (non-wire) form: a discriminated dict, the shape the trained
    # artifacts serialize after Phase 3. PE marks discriminate on the same `kind` field.
    assert _ADAPTER.validate_python({"kind": "private_equity_mark", "issuer_id": "openai"}) == PrivateEquityMarkKey(
        issuer_id=IssuerId("openai")
    )
    assert _ADAPTER.validate_python({"kind": "home_value", "location_id": "san_francisco_ca"}) == HomeValueKey(
        location_id=LocationId("san_francisco_ca")
    )
    assert _ADAPTER.validate_python({"kind": "sp500"}) == SP500Key()


def test_parse_factor_key_rejects_unknown_wire_id() -> None:
    with pytest.raises(ValueError, match="neither a level series nor a private-equity mark"):
        parse_factor_key("mystery:thing")


def test_extra_forbid_rejects_stray_keys() -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"kind": "private_equity_mark", "issuer_id": "openai", "stray": 1})


if __name__ == "__main__":
    pytest_bazel.main()
