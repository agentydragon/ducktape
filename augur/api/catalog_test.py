"""Catalog/settings builder tests for public-safe fixture composition."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel
from more_itertools import one

from augur.api.catalog import build_catalog, build_settings
from augur.api.config import AgentDefinition, Config, LocationConfig, PropertyAssetConfig, PropertySourceConfig
from augur.api.finance import FinanceSnapshot
from augur.api.local_regulation import LocalRegulation, TaxRegime
from augur.api.portfolio_source_config import FixedPortfolioSourceConfig, PortfolioSourcesConfig
from augur.api.wire import ActorRole
from augur.model.independent import IndependentProviderConfig


def _write_properties(path: Path) -> None:
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
                    "price_usd": 900000,
                    "rent_estimate_usd": 4200,
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
                    "price_usd": 520000,
                    "rent_estimate_usd": 3100,
                    "beds": 3,
                    "baths": 2,
                    "sqft": 1250,
                    "year_built": 2000,
                },
            ]
        ),
        encoding="utf-8",
    )


def _write_builtin_properties(path: Path) -> None:
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
                    "price_usd": 900000,
                    "rent_estimate_usd": 4200,
                    "beds": 3,
                    "baths": 2,
                    "sqft": 1400,
                    "year_built": 2000,
                }
            ]
        ),
        encoding="utf-8",
    )


def _fixture_regulation() -> LocalRegulation:
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


def _san_francisco_regulation() -> LocalRegulation:
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


def _fixture_locations() -> tuple[LocationConfig, ...]:
    regulation = _fixture_regulation()
    return (
        LocationConfig(
            location_id="location_a",
            label="Location A",
            city="Location A",
            state="Fixture",
            local_regulation=regulation,
            notes=("Synthetic public fixture location.",),
        ),
        LocationConfig(
            location_id="location_b",
            label="Location B",
            city="Location B",
            state="Fixture",
            local_regulation=regulation,
            notes=("Synthetic public fixture location.",),
        ),
        LocationConfig(
            location_id="san_francisco_ca",
            label="San Francisco, CA",
            city="San Francisco",
            state="CA",
            local_regulation=_san_francisco_regulation(),
            notes=("San Francisco fixture.",),
        ),
    )


def _config(
    properties_path: Path,
    *,
    location_selection: tuple[str, ...] | None = None,
    property_assets: tuple[PropertyAssetConfig, ...] = (),
) -> Config:
    return Config(
        agents=(AgentDefinition(actor_id="agent_a", label="Agent A", role=ActorRole.PRIMARY_OWNER),),
        property_source=PropertySourceConfig(properties_path=properties_path, property_assets=property_assets),
        portfolio_sources=PortfolioSourcesConfig(
            fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-14", cash_usd=12_345))
        ),
        default_rollout_samples=8,
        max_rollout_samples=128,
        locations=_fixture_locations(),
        location_selection=location_selection,
        models={"current_model": IndependentProviderConfig()},
        default_model_id="current_model",
    )


def _property_by_id(catalog, property_id: str):
    return one(property_ for property_ in catalog.properties if property_.id == property_id)


def test_catalog_locations_default_to_loaded_property_source(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    catalog = build_catalog(_config(properties_path))

    assert [location.id for location in catalog.locations] == ["location_a", "location_b"]
    assert [property_.id for property_ in catalog.properties] == ["location_a_property", "location_b_property"]


def test_catalog_san_francisco_location_carries_modeled_tax_defaults(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_builtin_properties(properties_path)

    catalog = build_catalog(_config(properties_path))
    location = one(loc for loc in catalog.locations if loc.id == "san_francisco_ca")

    assert location.label == "San Francisco, CA"
    assert location.city == "San Francisco"
    assert location.local_regulation.property_tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert TaxRegime.SAN_FRANCISCO_TRANSFER_TAX in location.local_regulation.default_tax_regimes


def test_catalog_applies_public_property_asset_urls(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    catalog = build_catalog(
        _config(
            properties_path,
            property_assets=(
                PropertyAssetConfig(
                    property_id="location_a_property", image_url="https://cdn.example.com/augur/location-a-hero.jpg"
                ),
            ),
        )
    )

    assert (
        _property_by_id(catalog, "location_a_property").image_url == "https://cdn.example.com/augur/location-a-hero.jpg"
    )
    assert _property_by_id(catalog, "location_b_property").image_url is None


def test_catalog_allows_explicit_public_property_asset_url(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    catalog = build_catalog(
        _config(
            properties_path,
            property_assets=(
                PropertyAssetConfig(
                    property_id="location_b_property", image_url="https://cdn.example.com/augur/location-b-hero.jpg"
                ),
            ),
        )
    )

    assert _property_by_id(catalog, "location_b_property").image_url == (
        "https://cdn.example.com/augur/location-b-hero.jpg"
    )


def test_catalog_location_selection_filters_properties_and_locations(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    catalog = build_catalog(_config(properties_path, location_selection=("location_a",)))

    assert [location.id for location in catalog.locations] == ["location_a"]
    assert [property_.id for property_ in catalog.properties] == ["location_a_property"]


def test_settings_carries_sampling_limits(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    settings = build_settings(_config(properties_path))

    assert settings.max_rollout_samples == 128
    assert settings.max_horizon_months == 1200


def test_catalog_rejects_unknown_property_location(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)
    records = json.loads(properties_path.read_text(encoding="utf-8"))
    records[0]["location_id"] = "missing_location"
    properties_path.write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(
        ValueError, match="property 'location_a_property' references unknown location 'missing_location'"
    ):
        build_catalog(_config(properties_path))


def test_catalog_rejects_unknown_location_selection(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    with pytest.raises(ValueError, match="location_selection references unknown location ids"):
        build_catalog(_config(properties_path, location_selection=("missing_location",)))


def test_catalog_rejects_duplicate_config_location_ids(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)
    config = _config(properties_path).model_copy(
        update={"locations": (_fixture_locations()[0], _fixture_locations()[0])}
    )

    with pytest.raises(ValueError, match="duplicate location ids"):
        build_catalog(config)


def test_catalog_rejects_asset_for_unknown_property(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    with pytest.raises(ValueError, match="property_assets reference unknown property ids"):
        build_catalog(
            _config(
                properties_path,
                property_assets=(
                    PropertyAssetConfig(
                        property_id="missing_property", image_url="https://cdn.example.com/augur/missing-hero.jpg"
                    ),
                ),
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
