from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from finance.augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from finance.augur.model.independent import IndependentProviderConfig
from finance.augur.model.provider_config import ProviderConfig
from finance.augur.model.series import SP500_SYMBOL, HomeValueKey, InflationKey, LocationId, RentKey, SecurityKey


@pytest.fixture
def example_config() -> IndependentProviderConfig:
    # Typed role config: no magic-prefix keys. Each level series sits inside its
    # role sub-group (asset_prices / property_values / index_series); singletons are
    # scalar, security/home_value/rent are keyed by sub-id. PE marks live in their own
    # issuer-keyed map (they are not level series).
    return IndependentProviderConfig.model_validate(
        {
            "type": "independent",
            "asset_prices": {
                "security": {
                    "SPY": {
                        "kind": "gbm",
                        "initial_value": 1.0,
                        "monthly_log_return_mu": 0.0047333327,
                        "monthly_log_return_sigma": 0.0461880215,
                    }
                }
            },
            "property_values": {
                "home_value": {
                    "san_francisco_ca": {
                        "kind": "gbm",
                        "initial_value": 1.0,
                        "monthly_log_return_mu": 0.0026498025,
                        "monthly_log_return_sigma": 0.0230940108,
                    }
                }
            },
            "index_series": {
                "inflation": {
                    "kind": "gbm",
                    "initial_value": 1.0,
                    "monthly_log_return_mu": 0.0024906250,
                    "monthly_log_return_sigma": 0.0043301270,
                },
                "rent": {
                    "san_francisco_ca": {
                        "kind": "gbm",
                        "initial_value": 1.0,
                        "monthly_log_return_mu": 0.0024625000,
                        "monthly_log_return_sigma": 0.0086602540,
                    }
                },
            },
            "private_equity_marks": {
                "private_equity_x": {
                    "kind": "gbm",
                    "initial_value": 50.0,
                    "monthly_log_return_mu": 0.0015629326,
                    "monthly_log_return_sigma": 0.1010362971,
                }
            },
        }
    )


def test_independent_model_samples_levels_and_events(example_config: IndependentProviderConfig) -> None:
    model = example_config.realize_model()

    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=12,
            rollout_seeds=(7, 8),
            **level_series_request_channels(
                frozenset(
                    {
                        InflationKey(),
                        SecurityKey(symbol=SP500_SYMBOL),
                        HomeValueKey(location_id=LocationId("san_francisco_ca")),
                        RentKey(location_id=LocationId("san_francisco_ca")),
                    }
                )
            ),
        )
    )

    assert sampled.levels.series_keys() == {
        InflationKey(),
        SecurityKey(symbol=SP500_SYMBOL),
        HomeValueKey(location_id=LocationId("san_francisco_ca")),
        RentKey(location_id=LocationId("san_francisco_ca")),
    }
    # IndependentModel doesn't sample PE channels — the typed PE bundle stays empty.
    assert sampled.private_equity.is_empty()
    assert sampled.model_id == "independent"
    # The PE mark's month-0 initial_value is surfaced as a typed field, keyed by issuer.
    assert sampled.private_equity_prices_usd == {"private_equity_x": 50.0}


def test_independent_provider_config_roundtrips_through_discriminated_union(
    example_config: IndependentProviderConfig,
) -> None:
    adapter: TypeAdapter[ProviderConfig] = TypeAdapter(ProviderConfig)
    config = adapter.validate_python(example_config.model_dump())
    assert isinstance(config, IndependentProviderConfig)
    sampled = config.realize_model().sample(ExogenousSamplingRequest(horizon_months=3, rollout_seeds=(9,)))
    assert sampled.private_equity_prices_usd == {"private_equity_x": 50.0}


def test_realized_model_keeps_role_structure(example_config: IndependentProviderConfig) -> None:
    # The runtime model holds level specs as the role sub-groups (same shape
    # as config / the sampled bundle), not a flattened opaque key map. The config-only
    # `private_equity_marks` sibling travels separately as `pe_marks`.
    model = example_config.realize_model()
    assert model.index_series.inflation is not None
    assert set(model.asset_prices.security) == {"SPY"}
    assert set(model.property_values.home_value) == {"san_francisco_ca"}
    assert set(model.index_series.rent) == {"san_francisco_ca"}
    assert set(model.pe_marks) == {"private_equity_x"}
    # The level series surface as typed LevelSeriesKeys, one per series across all roles.
    # Through `emittable_level_keys`, which is the only thing that ever asked: this provider
    # is per-series independent, so it has no factor basis to expose and no longer pretends to.
    assert {key.wire_id for key in model.emittable_level_keys()} == {
        "inflation",
        "security:SPY",
        "home_value:san_francisco_ca",
        "rent:san_francisco_ca",
    }


def test_legacy_prefix_keys_are_rejected() -> None:
    # The whole point of the typed shape: an old-style wire-id key at the top
    # level must fail loudly (extra="forbid"), not be silently prefix-parsed.
    with pytest.raises(ValidationError):
        IndependentProviderConfig.model_validate(
            {"type": "independent", "security:btc": {"kind": "constant", "value": 75000.0}}
        )


if __name__ == "__main__":
    pytest_bazel.main()
