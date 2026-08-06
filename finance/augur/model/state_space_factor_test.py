from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from finance.augur.model.series import (
    SP500_SYMBOL,
    HomeValueKey,
    InflationKey,
    IssuerId,
    LevelSeriesKind,
    LocationId,
    RentKey,
    SecurityKey,
)
from finance.augur.model.state_space_factor import FactorKey, PrivateEquityMarkKey, parse_factor_key

_ADAPTER: TypeAdapter[FactorKey] = TypeAdapter(FactorKey)

# One structural example per level kind, so the coverage test below can assert that each is
# accepted or rejected rather than only that the kind was thought about.
_EXAMPLE_BY_KIND: dict[LevelSeriesKind, dict[str, str]] = {
    LevelSeriesKind.INFLATION: {"kind": "inflation"},
    LevelSeriesKind.SECURITY: {"kind": "security", "symbol": "SPY"},
    LevelSeriesKind.SECURITY_DISTRIBUTION: {"kind": "security_distribution", "symbol": "bnd"},
    LevelSeriesKind.HOME_VALUE: {"kind": "home_value", "location_id": "san_francisco_ca"},
    LevelSeriesKind.RENT: {"kind": "rent", "location_id": "vallejo_ca"},
}


def test_parse_factor_key_decodes_level_series() -> None:
    assert parse_factor_key("inflation") == InflationKey()
    assert parse_factor_key("security:SPY") == SecurityKey(symbol=SP500_SYMBOL)
    assert parse_factor_key("security:btc") == SecurityKey(symbol="btc")
    assert parse_factor_key("home_value:san_francisco_ca") == HomeValueKey(location_id=LocationId("san_francisco_ca"))
    assert parse_factor_key("rent:vallejo_ca") == RentKey(location_id=LocationId("vallejo_ca"))


def test_parse_factor_key_decodes_private_equity_mark() -> None:
    assert parse_factor_key("private_equity:openai") == PrivateEquityMarkKey(issuer_id=IssuerId("openai"))


def test_wire_id_round_trips_through_parse() -> None:
    keys: list[FactorKey] = [
        InflationKey(),
        SecurityKey(symbol=SP500_SYMBOL),
        SecurityKey(symbol="btc"),
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
    assert _ADAPTER.validate_python({"kind": "security", "symbol": "SPY"}) == SecurityKey(symbol=SP500_SYMBOL)


def test_parse_factor_key_rejects_unknown_wire_id() -> None:
    with pytest.raises(ValueError, match="neither a level series nor a private-equity mark"):
        parse_factor_key("mystery:thing")


def test_parse_factor_key_rejects_an_emittable_series_this_model_does_not_fit() -> None:
    """A level kind added for another provider is not automatically a state-space factor.

    `security_distribution` is emittable and has a valid wire id, so it decodes fine as a level
    key — and would land in the covariance basis by default, where `_coupling_allowed` falls
    through and gives it half its empirical correlation to every macro factor. Being emittable
    is not the same as being fitted here.
    """

    with pytest.raises(ValueError, match="does not fit"):
        parse_factor_key("security_distribution:bnd")


def test_factor_key_covers_every_level_kind_deliberately() -> None:
    """Fails when a `LevelSeriesKind` is added, which is the point.

    `FactorKey` lists its level members explicitly rather than deriving them from the level-key
    union, so that a new emission kind cannot join this model's basis by default. The cost of
    that choice is drift, and this is what catches it: adding a kind fails here until someone
    decides which side it belongs on and records the decision in this set.
    """

    fitted = {LevelSeriesKind.INFLATION, LevelSeriesKind.SECURITY, LevelSeriesKind.HOME_VALUE, LevelSeriesKind.RENT}
    not_fitted = {LevelSeriesKind.SECURITY_DISTRIBUTION}

    assert fitted | not_fitted == set(LevelSeriesKind), (
        "a LevelSeriesKind was added without deciding whether the state-space model fits it. "
        "If it does, add it to FactorKey and to `fitted` here; if it does not, add it to `not_fitted`."
    )
    for kind in fitted:
        assert _ADAPTER.validate_python(_EXAMPLE_BY_KIND[kind]).kind == kind
    for kind in not_fitted:
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python(_EXAMPLE_BY_KIND[kind])


def test_extra_forbid_rejects_stray_keys() -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"kind": "private_equity_mark", "issuer_id": "openai", "stray": 1})


if __name__ == "__main__":
    pytest_bazel.main()
