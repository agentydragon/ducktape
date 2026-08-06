"""Schema-level checks for Config. Verifies the contract a deployment
must satisfy without exercising any actual file loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.api.config import (
    AgentDefinition,
    CalibrationCatalogConfig,
    Config,
    DistributionTaxShareConfig,
    LocationConfig,
    PropertyAssetConfig,
    PropertySourceConfig,
    SecurityDistributionConfig,
    dump_augur_config_yaml,
    load_augur_config,
)
from finance.augur.api.conftest import MinimalConfig
from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.local_regulation import LocalRegulation
from finance.augur.api.portfolio import (
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.api.portfolio_source_config import (
    FixedPortfolioSourceConfig,
    PlaidCashSourceConfig,
    PlaidPortfolioSourceConfig,
    PlaidSp500ProxyGroupConfig,
    PortfolioSourcesConfig,
)
from finance.augur.api.wire import ActorRole
from finance.augur.model.independent import IndependentProviderConfig
from finance.augur.model.private_equity_risk import PrivateEquityRiskProviderConfig
from finance.augur.model.provider_config import CompositeProviderConfig
from finance.augur.model.series import IssuerId, SecuritySymbol
from finance.augur.model.state_space import StateSpaceProviderConfig
from finance.augur.model.trained_private_equity import TrainedPrivateEquityProviderConfig


def test_minimal_config_validates_with_explicit_sampling_config(minimal_config: MinimalConfig) -> None:
    config = minimal_config()

    assert config.agents[0].actor_id == "owner"
    assert config.location_selection is None
    assert config.default_rollout_samples == 128
    assert config.max_rollout_samples == 1_000_000


def test_sampling_config_is_required(minimal_config: MinimalConfig) -> None:
    base = minimal_config().model_dump(mode="json")
    with pytest.raises(ValidationError, match="default_rollout_samples"):
        Config.model_validate({**base, "default_rollout_samples": None})
    with pytest.raises(ValidationError, match="max_rollout_samples"):
        Config.model_validate({**base, "max_rollout_samples": None})


def test_property_source_declares_stable_public_asset_urls() -> None:
    source = PropertySourceConfig(
        properties_path="/tmp/properties.json",
        property_assets=(
            PropertyAssetConfig(
                property_id="location_a_property", image_url="https://cdn.example.com/augur/location-a-hero.jpg"
            ),
            PropertyAssetConfig(
                property_id="location_b_property", image_url="https://cdn.example.com/augur/location-b-hero.jpg"
            ),
        ),
    )

    assert str(source.property_assets[0].image_url) == "https://cdn.example.com/augur/location-a-hero.jpg"
    assert str(source.property_assets[1].image_url) == "https://cdn.example.com/augur/location-b-hero.jpg"


def test_property_asset_requires_image_url() -> None:
    with pytest.raises(ValidationError, match="image_url"):
        PropertyAssetConfig.model_validate({"property_id": "location_a_property"})


def test_property_asset_property_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate property asset property_ids"):
        PropertySourceConfig(
            properties_path="/tmp/properties.json",
            property_assets=(
                PropertyAssetConfig(property_id="location_a_property", image_url="https://cdn.example.com/a.jpg"),
                PropertyAssetConfig(property_id="location_a_property", image_url="https://cdn.example.com/b.jpg"),
            ),
        )


def test_config_carries_tax_lot_accurate_portfolio_schema(minimal_config: MinimalConfig) -> None:
    config = minimal_config(
        portfolio_sources=PortfolioSourcesConfig(
            fixed=FixedPortfolioSourceConfig(
                snapshot=FinanceSnapshot(as_of_date="2026-05-12"),
                portfolio=PortfolioConfig(
                    accounts=(PortfolioAccountConfig(account_id="taxable_brokerage", owner_agent_id="owner"),),
                    holdings=(
                        SecurityHoldingConfig(
                            position_id="voo_position",
                            account_id="taxable_brokerage",
                            symbol=SecuritySymbol("VOO"),
                            security_kind="etf",
                            unit_value_usd=500.0,
                            lots=(
                                HoldingTaxLotConfig(
                                    lot_id="voo_2024_05_12",
                                    holding_period_months_at_start=24,
                                    quantity=100,
                                    cost_basis_usd=30_000,
                                ),
                            ),
                        ),
                    ),
                ),
            )
        )
    )

    reloaded = Config.model_validate_json(config.model_dump_json(exclude_computed_fields=True))

    fixed = reloaded.portfolio_sources.fixed
    assert fixed.portfolio.holdings[0].lots[0].holding_period_months_at_start == 24
    assert fixed.portfolio.to_initial_lots()[0].purchase_month_index == -24


def test_config_carries_optional_plaid_portfolio_source(minimal_config: MinimalConfig) -> None:
    config = minimal_config(
        portfolio_sources=PortfolioSourcesConfig(
            plaid=PlaidPortfolioSourceConfig(
                enabled=True,
                cash=PlaidCashSourceConfig(plaid_account_ids=("checking-account",)),
                sp500_proxy_groups=(
                    PlaidSp500ProxyGroupConfig(
                        position_id="wealthfront_sp500",
                        portfolio_account_id="wealthfront_taxable",
                        owner_agent_id="owner",
                        plaid_account_ids=("wealthfront-plaid-account",),
                    ),
                ),
            )
        )
    )

    reloaded = Config.model_validate_json(config.model_dump_json(exclude_computed_fields=True))

    assert reloaded.portfolio_sources.plaid.enabled is True
    assert reloaded.portfolio_sources.plaid.cash.plaid_account_ids == ("checking-account",)
    assert reloaded.portfolio_sources.plaid.sp500_proxy_groups[0].portfolio_account_id == "wealthfront_taxable"


def test_enabled_plaid_portfolio_source_must_select_something() -> None:
    with pytest.raises(ValidationError, match="must select cash accounts or SP500 proxy groups"):
        PlaidPortfolioSourceConfig(enabled=True)


def test_location_selection_accepts_location_strings(minimal_config: MinimalConfig) -> None:
    config = minimal_config(location_selection=("san_francisco_ca", "vallejo_ca"))

    assert config.location_selection == ("san_francisco_ca", "vallejo_ca")


def test_config_can_define_deployment_owned_locations(
    minimal_config: MinimalConfig, fixture_regulation: LocalRegulation
) -> None:
    config = minimal_config(
        locations=(
            LocationConfig(
                location_id="location_a",
                label="Location A",
                city="Location A",
                state="Fixture",
                local_regulation=fixture_regulation,
            ),
        ),
        location_selection=("location_a",),
    )

    assert config.locations[0].location_id == "location_a"
    assert config.location_selection == ("location_a",)


def test_at_least_one_agent_required() -> None:
    with pytest.raises(ValidationError, match="Tuple should have at least 1 item"):
        Config(
            agents=(),
            property_source=PropertySourceConfig(properties_path="/tmp/x.json"),
            portfolio_sources=PortfolioSourcesConfig(
                fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-12"))
            ),
            default_rollout_samples=128,
            max_rollout_samples=1_000_000,
            models={"current_model": IndependentProviderConfig()},
            default_model_id="current_model",
            calibration_catalog=CalibrationCatalogConfig(catalog_path=Path("/tmp/catalog.yaml")),
        )


def test_actor_id_must_be_snake_case() -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        AgentDefinition(actor_id="Alpha", label="Alpha", role=ActorRole.PRIMARY_OWNER)


def test_snapshot_optional_fields_default_to_zero() -> None:
    snapshot = FinanceSnapshot(as_of_date="2026-05-12")
    assert snapshot.cash_usd == 0.0


def test_unknown_field_is_rejected(minimal_config: MinimalConfig) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        minimal_config(extra_field="nope")


def test_yaml_round_trip_through_dump_and_load(tmp_path: Path, minimal_config: MinimalConfig) -> None:
    config = minimal_config(location_selection=("san_francisco_ca",))

    path = tmp_path / "config.yaml"
    path.write_text(dump_augur_config_yaml(config), encoding="utf-8")
    reloaded = load_augur_config(path)

    assert reloaded == config


def test_config_accepts_composite_provider_with_trained_private_equity(
    tmp_path: Path, minimal_config: MinimalConfig
) -> None:
    model_path = tmp_path / "private_equity_model.json"
    config = minimal_config(
        models={
            "current_model": {
                "type": "composite",
                "macro": {"type": "independent"},
                "private_equity": {"type": "trained_private_equity", "trained_model_path": str(model_path)},
            }
        }
    )

    provider = config.models[config.default_model_id]
    assert isinstance(provider, CompositeProviderConfig)
    assert isinstance(provider.private_equity, TrainedPrivateEquityProviderConfig)
    assert provider.private_equity.trained_model_path == model_path


def test_config_accepts_composite_provider_with_private_equity_risk(minimal_config: MinimalConfig) -> None:
    config = minimal_config(
        models={
            "current_model": {
                "type": "composite",
                "macro": {"type": "independent"},
                "private_equity": {
                    "type": "private_equity_risk",
                    "issuers": {"private_holding_a": {"current_mark_usd": 25.0}},
                },
            }
        }
    )

    provider = config.models[config.default_model_id]
    assert isinstance(provider, CompositeProviderConfig)
    assert isinstance(provider.private_equity, PrivateEquityRiskProviderConfig)
    assert provider.private_equity.issuers[IssuerId("private_holding_a")].current_mark_usd == 25.0


def test_relative_trained_private_equity_model_path_anchors_against_yaml_dir(
    tmp_path: Path, minimal_config: MinimalConfig
) -> None:
    (tmp_path / "properties.json").write_text("[]", encoding="utf-8")
    (tmp_path / "private_equity_model.json").write_text("{}", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        dump_augur_config_yaml(
            minimal_config(
                property_source=PropertySourceConfig(properties_path=Path("properties.json")),
                models={
                    "current_model": {
                        "type": "composite",
                        "macro": {"type": "independent"},
                        "private_equity": {
                            "type": "trained_private_equity",
                            "trained_model_path": "private_equity_model.json",
                        },
                    }
                },
            )
        ),
        encoding="utf-8",
    )

    reloaded = load_augur_config(config_path)

    provider = reloaded.models[reloaded.default_model_id]
    assert isinstance(provider, CompositeProviderConfig)
    assert isinstance(provider.private_equity, TrainedPrivateEquityProviderConfig)
    assert provider.private_equity.trained_model_path == (tmp_path / "private_equity_model.json").resolve()


def test_relative_state_space_artifact_path_anchors_against_yaml_dir(
    tmp_path: Path, minimal_config: MinimalConfig
) -> None:
    (tmp_path / "properties.json").write_text("[]", encoding="utf-8")
    (tmp_path / "state_space_artifact.json").write_text("{}", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        dump_augur_config_yaml(
            minimal_config(
                property_source=PropertySourceConfig(properties_path=Path("properties.json")),
                models={
                    "current_model": {
                        "type": "state_space",
                        "trained_artifact_path": "state_space_artifact.json",
                        "conditioning": {"start_at": "2026-05-27", "observations": {}},
                        "current_mortgage30_rate_pct": 6.23,
                    }
                },
            )
        ),
        encoding="utf-8",
    )

    reloaded = load_augur_config(config_path)

    provider = reloaded.models[reloaded.default_model_id]
    assert isinstance(provider, StateSpaceProviderConfig)
    assert provider.trained_artifact_path == (tmp_path / "state_space_artifact.json").resolve()


def test_calibration_catalog_sample_sanity_path_defaults_to_none() -> None:
    catalog = CalibrationCatalogConfig(catalog_path=Path("/tmp/catalog.yaml"))
    assert catalog.sample_sanity_path is None


def test_relative_calibration_catalog_paths_anchor_against_yaml_dir(
    tmp_path: Path, minimal_config: MinimalConfig
) -> None:
    """Both `catalog_path` and the optional `sample_sanity_path` resolve against the yaml dir,
    like the other ConfigMap-mounted deployment paths."""
    (tmp_path / "properties.json").write_text("[]", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        dump_augur_config_yaml(
            minimal_config(
                property_source=PropertySourceConfig(properties_path=Path("properties.json")),
                calibration_catalog=CalibrationCatalogConfig(
                    catalog_path=Path("catalog.yaml"), sample_sanity_path=Path("sample_sanity.yaml")
                ),
            )
        ),
        encoding="utf-8",
    )

    reloaded = load_augur_config(config_path)

    assert reloaded.calibration_catalog.catalog_path == (tmp_path / "catalog.yaml").resolve()
    assert reloaded.calibration_catalog.sample_sanity_path == (tmp_path / "sample_sanity.yaml").resolve()


def test_relative_property_source_paths_anchor_against_yaml_dir(tmp_path: Path, minimal_config: MinimalConfig) -> None:
    """ConfigMap mounts put config.yaml + properties.json side-by-side, so the
    yaml stores `properties_path: properties.json` and the loader resolves
    against the yaml's directory."""
    (tmp_path / "properties.json").write_text("[]", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "config.yaml").write_text(
        dump_augur_config_yaml(
            minimal_config(
                property_source=PropertySourceConfig(
                    properties_path=Path("properties.json"),
                    asset_dir=Path("assets"),
                    property_assets=(
                        PropertyAssetConfig(
                            property_id="location_a_property",
                            image_url="https://cdn.example.com/augur/location-a-hero.jpg",
                        ),
                    ),
                )
            )
        ),
        encoding="utf-8",
    )

    reloaded = load_augur_config(tmp_path / "config.yaml")

    assert reloaded.property_source.properties_path == (tmp_path / "properties.json").resolve()
    assert reloaded.property_source.asset_dir == (tmp_path / "assets").resolve()
    assert (
        str(reloaded.property_source.property_assets[0].image_url)
        == "https://cdn.example.com/augur/location-a-hero.jpg"
    )


def test_a_security_distribution_must_allocate_its_whole_payout(minimal_config: MinimalConfig) -> None:
    """A short split pays out less than the fund distributes, which reads as a lower yield
    rather than as the misconfiguration it is."""

    with pytest.raises(ValidationError, match="fractions must sum to 1"):
        minimal_config(
            security_distributions=(
                SecurityDistributionConfig(
                    symbol=SecuritySymbol("bnd"),
                    tax_character=(DistributionTaxShareConfig(fraction=0.4, issuer_jurisdiction_id="federal_us"),),
                ),
            )
        )


def test_a_security_distribution_is_declared_once_per_symbol(minimal_config: MinimalConfig) -> None:
    """Two declarations for one fund cannot both be what it holds, and the pool would pay twice."""

    declaration = SecurityDistributionConfig(
        symbol=SecuritySymbol("bnd"), tax_character=(DistributionTaxShareConfig(fraction=1.0),)
    )

    with pytest.raises(ValidationError, match="name each symbol once"):
        minimal_config(security_distributions=(declaration, declaration))


if __name__ == "__main__":
    pytest_bazel.main()
