"""Schema-level checks for Config. Verifies the contract a deployment
must satisfy without exercising any actual file loading."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import HttpUrl, ValidationError

from finance.augur.api.config import (
    CalibrationCatalogConfig,
    DistributionTaxShareConfig,
    PropertyAssetConfig,
    PropertySourceConfig,
    SecurityDistributionConfig,
    dump_augur_config_yaml,
    load_augur_config,
)
from finance.augur.api.conftest import MinimalConfig
from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    HoldingKind,
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
from finance.augur.model.series import SecuritySymbol
from finance.augur.sim.ids import AccountId, AgentId, LotId, PropertyId
from finance.augur.sim.income import Taxable, Treasury
from finance.augur.x.models.provider_config import CompositeProviderConfig
from finance.augur.x.models.state_space import StateSpaceProviderConfig
from finance.augur.x.models.trained_private_equity import TrainedPrivateEquityProviderConfig

LOCATION_A_PROPERTY = PropertyId("location_a_property")

TAX_LOT_PORTFOLIO_SOURCES = PortfolioSourcesConfig(
    fixed=FixedPortfolioSourceConfig(
        snapshot=FinanceSnapshot(as_of_date="2026-05-12"),
        portfolio=PortfolioConfig(
            accounts=(
                PortfolioAccountConfig(account_id=AccountId("taxable_brokerage"), owner_agent_id=AgentId("owner")),
            ),
            holdings=(
                SecurityHoldingConfig(
                    position_id="voo_position",
                    account_id=AccountId("taxable_brokerage"),
                    symbol=SecuritySymbol("VOO"),
                    security_kind=HoldingKind.ETF,
                    unit_value=Decimal(500),
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id=LotId("voo_2024_05_12"),
                            holding_period_months_at_start=24,
                            quantity=100,
                            cost_basis=Decimal(30_000),
                        ),
                    ),
                ),
            ),
        ),
    )
)

PLAID_PORTFOLIO_SOURCES = PortfolioSourcesConfig(
    plaid=PlaidPortfolioSourceConfig(
        enabled=True,
        cash=PlaidCashSourceConfig(plaid_account_ids=("checking-account",)),
        sp500_proxy_groups=(
            PlaidSp500ProxyGroupConfig(
                position_id="wealthfront_sp500",
                portfolio_account_id=AccountId("wealthfront_taxable"),
                owner_agent_id=AgentId("owner"),
                plaid_account_ids=("wealthfront-plaid-account",),
            ),
        ),
    )
)


def test_property_asset_property_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        PropertySourceConfig(
            properties_path=Path("/tmp/properties.json"),
            property_assets=(
                PropertyAssetConfig(
                    property_id=LOCATION_A_PROPERTY, image_url=HttpUrl("https://cdn.example.com/a.jpg")
                ),
                PropertyAssetConfig(
                    property_id=LOCATION_A_PROPERTY, image_url=HttpUrl("https://cdn.example.com/b.jpg")
                ),
            ),
        )


def test_enabled_plaid_portfolio_source_must_select_something() -> None:
    with pytest.raises(ValidationError):
        PlaidPortfolioSourceConfig(enabled=True)


def test_unknown_field_is_rejected(minimal_config: MinimalConfig) -> None:
    with pytest.raises(ValidationError) as rejected:
        minimal_config(extra_field="nope")

    assert [(error["type"], error["loc"]) for error in rejected.value.errors()] == [
        ("extra_forbidden", ("extra_field",))
    ]


@pytest.mark.parametrize(
    "portfolio_sources",
    [None, TAX_LOT_PORTFOLIO_SOURCES, PLAID_PORTFOLIO_SOURCES],
    ids=["snapshot", "tax_lots", "plaid"],
)
def test_yaml_round_trip_through_dump_and_load(
    tmp_path: Path, minimal_config: MinimalConfig, portfolio_sources: PortfolioSourcesConfig | None
) -> None:
    config = minimal_config(location_selection=("san_francisco_ca",), portfolio_sources=portfolio_sources)

    path = tmp_path / "config.yaml"
    path.write_text(dump_augur_config_yaml(config), encoding="utf-8")
    reloaded = load_augur_config(path)

    assert reloaded == config


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
                            property_id=LOCATION_A_PROPERTY,
                            image_url=HttpUrl("https://cdn.example.com/augur/location-a-hero.jpg"),
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

    with pytest.raises(ValidationError):
        minimal_config(
            security_distributions=(
                SecurityDistributionConfig(
                    symbol=SecuritySymbol("bnd"),
                    tax_character=(DistributionTaxShareConfig(fraction=0.4, character=Treasury()),),
                ),
            )
        )


def test_a_security_distribution_is_declared_once_per_symbol(minimal_config: MinimalConfig) -> None:
    """Two declarations for one fund cannot both be what it holds, and the pool would pay twice."""

    declaration = SecurityDistributionConfig(
        symbol=SecuritySymbol("bnd"), tax_character=(DistributionTaxShareConfig(fraction=1.0, character=Taxable()),)
    )

    with pytest.raises(ValidationError):
        minimal_config(security_distributions=(declaration, declaration))


if __name__ == "__main__":
    pytest_bazel.main()
