from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from finance.augur.api.config import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    AgentDefinition,
    CalibrationCatalogConfig,
    Config,
    LocationConfig,
    PropertyAssetConfig,
    PropertySourceConfig,
)
from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.local_regulation import LocalRegulation, TaxRegime
from finance.augur.api.portfolio_source_config import (
    FixedPortfolioSourceConfig,
    PlaidCashSourceConfig,
    PlaidPortfolioSourceConfig,
    PlaidSp500ProxyGroupConfig,
    PortfolioSourcesConfig,
)
from finance.augur.api.server import ApiServerConfig, create_app, static_price_clients
from finance.augur.api.wire import ActorRole
from finance.augur.model.independent import IndependentProviderConfig
from finance.augur.model.provider_config import ProviderConfig
from finance.augur.model.testing import ConstantFrameModel

# Factories the fixtures below hand tests: build a Config (`minimal_config` overrides any field;
# `make_catalog_config` takes the property-shortlist path for the catalog-builder tests) or a
# TestClient over a given models map (`make_client`).
MinimalConfig = Callable[..., Config]
MakeCatalogConfig = Callable[..., Config]
MakeClient = Callable[[dict[str, Any]], TestClient]


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
def minimal_config(tmp_path: Path) -> MinimalConfig:
    """Factory for a minimal valid single-`owner` Config for schema-shape tests. Pass any field as
    a kwarg to override its default (`property_source`/`portfolio_sources`/`models`/
    `calibration_catalog` are typed; other Config fields ride through `**overrides`). File paths
    default under the test's `tmp_path` (nothing reads them in schema tests)."""

    def _make(
        *,
        property_source: PropertySourceConfig | None = None,
        portfolio_sources: PortfolioSourcesConfig | None = None,
        models: dict[str, Any] | None = None,
        calibration_catalog: CalibrationCatalogConfig | None = None,
        **overrides: object,
    ) -> Config:
        default_models: dict[str, ProviderConfig] = {"current_model": IndependentProviderConfig()}
        return Config(
            agents=(AgentDefinition(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER),),
            property_source=property_source
            if property_source is not None
            else PropertySourceConfig(properties_path=tmp_path / "properties.json"),
            portfolio_sources=portfolio_sources
            if portfolio_sources is not None
            else PortfolioSourcesConfig(
                fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-12"))
            ),
            max_rollout_samples=1_000_000,
            models=models if models is not None else default_models,
            default_model_id="current_model",
            calibration_catalog=calibration_catalog
            if calibration_catalog is not None
            else CalibrationCatalogConfig(catalog_path=tmp_path / "calibration_catalog.yaml"),
            **overrides,
        )

    return _make


@pytest.fixture
def plaid_config() -> PortfolioSourcesConfig:
    """A `PortfolioSourcesConfig` with an enabled Plaid source (one cash account + one SP500 proxy)."""
    return PortfolioSourcesConfig(
        fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-01", cash=100)),
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
                fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-14", cash=12_345))
            ),
            max_rollout_samples=128,
            locations=fixture_locations,
            location_selection=location_selection,
            models=models,
            default_model_id="current_model",
            calibration_catalog=CalibrationCatalogConfig(
                catalog_path=properties_path.parent / "calibration_catalog.yaml"
            ),
        )

    return _make


@pytest.fixture
def properties_path(tmp_path: Path) -> Path:
    """A two-location (location_a + location_b) property shortlist written to a temp JSON file."""
    path = tmp_path / "properties.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "location_a_property",
                    "source_catalog_id": "public_fixture",
                    "source_property_id": "location-a-property",
                    "location_id": "location_a",
                    "address": "Location A Property",
                    "neighborhood": "Location A",
                    "type": "Fixture",
                    "price": 900000,
                    "rent_estimate": 4200,
                    "beds": 3,
                    "baths": 2,
                    "sqft": 1400,
                    "year_built": 2000,
                },
                {
                    "id": "location_b_property",
                    "source_catalog_id": "public_fixture",
                    "source_property_id": "location-b-property",
                    "location_id": "location_b",
                    "address": "Location B Property",
                    "neighborhood": "Location B",
                    "type": "Fixture",
                    "price": 520000,
                    "rent_estimate": 3100,
                    "beds": 3,
                    "baths": 2,
                    "sqft": 1250,
                    "year_built": 2000,
                },
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def builtin_properties_path(tmp_path: Path) -> Path:
    """A single San-Francisco property shortlist written to a temp JSON file."""
    path = tmp_path / "properties.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "sf_property",
                    "source_catalog_id": "public_fixture",
                    "source_property_id": "sf-property",
                    "location_id": "san_francisco_ca",
                    "address": "SF Property",
                    "neighborhood": "San Francisco",
                    "type": "Fixture",
                    "price": 900000,
                    "rent_estimate": 4200,
                    "beds": 3,
                    "baths": 2,
                    "sqft": 1400,
                    "year_built": 2000,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def make_client(augur_config: Config) -> Iterator[MakeClient]:
    """Factory building a `TestClient` over the fixture deployment with the given models map.
    Every client it hands out is entered and torn down at fixture exit."""
    with contextlib.ExitStack() as stack:

        def _make(models: dict[str, Any]) -> TestClient:
            return stack.enter_context(
                TestClient(
                    create_app(
                        ApiServerConfig(
                            augur_config=augur_config, models=models, price_clients=static_price_clients({})
                        )
                    )
                )
            )

        yield _make


@pytest.fixture
def forced_private_equity_event_client(
    make_client: MakeClient, forced_private_equity_event_model: ConstantFrameModel
) -> TestClient:
    return make_client({"current_model": forced_private_equity_event_model})


@pytest.fixture
def capacity_limited_private_equity_client(
    make_client: MakeClient, capacity_limited_private_equity_model: ConstantFrameModel
) -> TestClient:
    return make_client({"current_model": capacity_limited_private_equity_model})
