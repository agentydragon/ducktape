from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from augur.api.config import AgentDefinition, Config, LocationConfig, PropertyAssetConfig, PropertySourceConfig
from augur.api.finance import FinanceSnapshot
from augur.api.local_regulation import LocalRegulation, TaxRegime
from augur.api.portfolio_source_config import (
    FixedPortfolioSourceConfig,
    PlaidCashSourceConfig,
    PlaidPortfolioSourceConfig,
    PlaidSp500ProxyGroupConfig,
    PortfolioSourcesConfig,
)
from augur.api.server import ApiServerConfig, create_app
from augur.api.wire import ActorRole
from augur.model.independent import IndependentProviderConfig
from augur.model.provider_config import ProviderConfig
from augur.product.testing import capacity_limited_private_equity_fixture, forced_private_equity_event_fixture

# Factories the fixtures below hand tests: build a Config (`minimal_config` overrides any field;
# `make_catalog_config` takes the property-shortlist path for the catalog-builder tests).
MinimalConfig = Callable[..., Config]
MakeCatalogConfig = Callable[..., Config]


@pytest.fixture
def fixture_regulation() -> LocalRegulation:
    """Synthetic CALIFORNIA_PROP13 regulation for the public-fixture locations."""
    return LocalRegulation(
        property_tax_regime=TaxRegime.CALIFORNIA_PROP13,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
        ),
        property_tax_annual_pct=1.0,
        notes="Synthetic public fixture location.",
    )


@pytest.fixture
def san_francisco_regulation() -> LocalRegulation:
    """San Francisco secured-property-tax regulation (the real SF regime stack)."""
    return LocalRegulation(
        property_tax_regime=TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
            TaxRegime.SAN_FRANCISCO_TRANSFER_TAX,
        ),
        property_tax_annual_pct=1.18,
        notes="San Francisco fixture",
    )


@pytest.fixture
def fixture_locations(
    fixture_regulation: LocalRegulation, san_francisco_regulation: LocalRegulation
) -> tuple[LocationConfig, ...]:
    """The two synthetic fixture locations plus San Francisco, for the catalog-builder tests."""
    return (
        LocationConfig(
            location_id="location_a",
            label="Location A",
            city="Location A",
            state="Fixture",
            local_regulation=fixture_regulation,
            notes=("Synthetic public fixture location.",),
        ),
        LocationConfig(
            location_id="location_b",
            label="Location B",
            city="Location B",
            state="Fixture",
            local_regulation=fixture_regulation,
            notes=("Synthetic public fixture location.",),
        ),
        LocationConfig(
            location_id="san_francisco_ca",
            label="San Francisco, CA",
            city="San Francisco",
            state="CA",
            local_regulation=san_francisco_regulation,
            notes=("San Francisco fixture.",),
        ),
    )


@pytest.fixture
def minimal_config() -> MinimalConfig:
    """Factory for a minimal valid single-`owner` Config for schema-shape tests. Pass any field as
    a kwarg to override its default (`property_source`/`portfolio_sources`/`models` are typed;
    other Config fields ride through `**overrides`)."""

    def _make(
        *,
        property_source: PropertySourceConfig | None = None,
        portfolio_sources: PortfolioSourcesConfig | None = None,
        models: dict[str, Any] | None = None,
        **overrides: object,
    ) -> Config:
        default_models: dict[str, ProviderConfig] = {"current_model": IndependentProviderConfig()}
        return Config(
            agents=(AgentDefinition(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER),),
            property_source=property_source
            if property_source is not None
            else PropertySourceConfig(properties_path="/tmp/properties.json"),
            portfolio_sources=portfolio_sources
            if portfolio_sources is not None
            else PortfolioSourcesConfig(
                fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-12"))
            ),
            default_rollout_samples=128,
            max_rollout_samples=1_000_000,
            models=models if models is not None else default_models,
            default_model_id="current_model",
            **overrides,
        )

    return _make


@pytest.fixture
def plaid_config() -> PortfolioSourcesConfig:
    """A `PortfolioSourcesConfig` with an enabled Plaid source (one cash account + one SP500 proxy)."""
    return PortfolioSourcesConfig(
        fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-01", cash_usd=100.0)),
        plaid=PlaidPortfolioSourceConfig(
            enabled=True,
            cash=PlaidCashSourceConfig(plaid_account_ids=("checking",)),
            sp500_proxy_groups=(
                PlaidSp500ProxyGroupConfig(
                    position_id="wealthfront_sp500",
                    portfolio_account_id="wealthfront_taxable",
                    owner_agent_id="owner",
                    account_label="Wealthfront",
                    label="SP500 proxy",
                    plaid_account_ids=("wealthfront_account",),
                    default_holding_period_months_at_start=24,
                ),
            ),
        ),
    )


@pytest.fixture
def make_catalog_config(fixture_locations: tuple[LocationConfig, ...]) -> MakeCatalogConfig:
    """Factory for a deployment Config over the fixture locations, given a property-shortlist path."""

    def _make(
        properties_path: Path,
        *,
        location_selection: tuple[str, ...] | None = None,
        property_assets: tuple[PropertyAssetConfig, ...] = (),
    ) -> Config:
        models: dict[str, ProviderConfig] = {"current_model": IndependentProviderConfig()}
        return Config(
            agents=(AgentDefinition(actor_id="agent_a", label="Agent A", role=ActorRole.PRIMARY_OWNER),),
            property_source=PropertySourceConfig(properties_path=properties_path, property_assets=property_assets),
            portfolio_sources=PortfolioSourcesConfig(
                fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-14", cash_usd=12_345))
            ),
            default_rollout_samples=8,
            max_rollout_samples=128,
            locations=fixture_locations,
            location_selection=location_selection,
            models=models,
            default_model_id="current_model",
        )

    return _make


@pytest.fixture
def forced_private_equity_event_client(augur_config: Config) -> Iterator[TestClient]:
    with _client_with(augur_config, {"current_model": forced_private_equity_event_fixture()}) as client:
        yield client


@pytest.fixture
def capacity_limited_private_equity_client(augur_config: Config) -> Iterator[TestClient]:
    with _client_with(augur_config, {"current_model": capacity_limited_private_equity_fixture()}) as client:
        yield client


def _client_with(augur_config: Config, models: dict[str, Any]) -> TestClient:
    return TestClient(create_app(ApiServerConfig(augur_config=augur_config, models=models, price_clients={})))
