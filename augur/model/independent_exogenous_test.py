from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from augur.model.exogenous import ExogenousSamplingRequest
from augur.model.exogenous_provider_config import ExogenousProviderConfig
from augur.model.independent_exogenous import IndependentExogenousProviderConfig
from augur.model.series import HomeValueKey, InflationKey, LocationId, RentKey, SP500Key


def _example_config() -> IndependentExogenousProviderConfig:
    # Typed per-kind config: no magic-prefix keys. Singletons are scalar;
    # home_value/rent are keyed by location sub-id; PE marks live in their own
    # issuer-keyed map (they are not level series).
    return IndependentExogenousProviderConfig.model_validate(
        {
            "type": "independent",
            "inflation": {
                "kind": "gbm",
                "initial_value": 1.0,
                "monthly_log_return_mu": 0.0024906250,
                "monthly_log_return_sigma": 0.0043301270,
            },
            "sp500": {
                "kind": "gbm",
                "initial_value": 1.0,
                "monthly_log_return_mu": 0.0047333327,
                "monthly_log_return_sigma": 0.0461880215,
            },
            "home_value": {
                "san_francisco_ca": {
                    "kind": "gbm",
                    "initial_value": 1.0,
                    "monthly_log_return_mu": 0.0026498025,
                    "monthly_log_return_sigma": 0.0230940108,
                }
            },
            "rent": {
                "san_francisco_ca": {
                    "kind": "gbm",
                    "initial_value": 1.0,
                    "monthly_log_return_mu": 0.0024625000,
                    "monthly_log_return_sigma": 0.0086602540,
                }
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


def test_independent_model_samples_levels_and_events() -> None:
    model = _example_config().realize_model()

    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=12,
            rollout_seeds=(7, 8),
            required_level_series=frozenset(
                {
                    InflationKey(),
                    SP500Key(),
                    HomeValueKey(location_id=LocationId("san_francisco_ca")),
                    RentKey(location_id=LocationId("san_francisco_ca")),
                }
            ),
        )
    )

    assert set(sampled.levels.get_column("series_id").unique()) == {
        InflationKey().wire_id,
        SP500Key().wire_id,
        HomeValueKey(location_id=LocationId("san_francisco_ca")).wire_id,
        RentKey(location_id=LocationId("san_francisco_ca")).wire_id,
    }
    # IndependentExogenousModel doesn't sample PE channels — the typed PE bundle stays empty.
    assert sampled.private_equity.is_empty()
    assert sampled.metadata["exogenous_model_id"] == "independent_exogenous_model"
    # The PE mark's month-0 initial_value is surfaced via metadata, keyed by issuer.
    assert sampled.metadata["private_equity_prices_usd"] == {"private_equity_x": 50.0}


def test_independent_provider_config_roundtrips_through_discriminated_union() -> None:
    adapter: TypeAdapter[ExogenousProviderConfig] = TypeAdapter(ExogenousProviderConfig)
    config = adapter.validate_python(_example_config().model_dump())
    assert isinstance(config, IndependentExogenousProviderConfig)
    assert config.realize_model().sample(ExogenousSamplingRequest(horizon_months=3, rollout_seeds=(9,))).metadata[
        "private_equity_prices_usd"
    ] == {"private_equity_x": 50.0}


def test_legacy_prefix_keys_are_rejected() -> None:
    # The whole point of the typed shape: an old-style wire-id key at the top
    # level must fail loudly (extra="forbid"), not be silently prefix-parsed.
    with pytest.raises(ValidationError):
        IndependentExogenousProviderConfig.model_validate(
            {"type": "independent", "crypto:btc": {"kind": "constant", "value": 75000.0}}
        )


if __name__ == "__main__":
    pytest_bazel.main()
