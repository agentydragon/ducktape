"""Catalog/settings builder tests for public-safe fixture composition."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel
from more_itertools import one

from augur.api.catalog import build_catalog, build_settings
from augur.api.config import LocationConfig, PropertyAssetConfig
from augur.api.conftest import MakeCatalogConfig
from augur.api.local_regulation import TaxRegime


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


def _property_by_id(catalog, property_id: str):
    return one(property_ for property_ in catalog.properties if property_.id == property_id)


def test_catalog_locations_default_to_loaded_property_source(
    tmp_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    catalog = build_catalog(make_catalog_config(properties_path))

    assert [location.id for location in catalog.locations] == ["location_a", "location_b"]
    assert [property_.id for property_ in catalog.properties] == ["location_a_property", "location_b_property"]


def test_catalog_san_francisco_location_carries_modeled_tax_defaults(
    tmp_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    properties_path = tmp_path / "properties.json"
    _write_builtin_properties(properties_path)

    catalog = build_catalog(make_catalog_config(properties_path))
    location = one(loc for loc in catalog.locations if loc.id == "san_francisco_ca")

    assert location.label == "San Francisco, CA"
    assert location.city == "San Francisco"
    assert location.local_regulation.property_tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert TaxRegime.SAN_FRANCISCO_TRANSFER_TAX in location.local_regulation.default_tax_regimes


def test_catalog_applies_public_property_asset_urls(tmp_path: Path, make_catalog_config: MakeCatalogConfig) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    catalog = build_catalog(
        make_catalog_config(
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


def test_catalog_allows_explicit_public_property_asset_url(
    tmp_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    catalog = build_catalog(
        make_catalog_config(
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


def test_catalog_location_selection_filters_properties_and_locations(
    tmp_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    catalog = build_catalog(make_catalog_config(properties_path, location_selection=("location_a",)))

    assert [location.id for location in catalog.locations] == ["location_a"]
    assert [property_.id for property_ in catalog.properties] == ["location_a_property"]


def test_settings_carries_sampling_limits(tmp_path: Path, make_catalog_config: MakeCatalogConfig) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    settings = build_settings(make_catalog_config(properties_path))

    assert settings.max_rollout_samples == 128
    assert settings.max_horizon_months == 1200


def test_catalog_rejects_unknown_property_location(tmp_path: Path, make_catalog_config: MakeCatalogConfig) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)
    records = json.loads(properties_path.read_text(encoding="utf-8"))
    records[0]["location_id"] = "missing_location"
    properties_path.write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(
        ValueError, match="property 'location_a_property' references unknown location 'missing_location'"
    ):
        build_catalog(make_catalog_config(properties_path))


def test_catalog_rejects_unknown_location_selection(tmp_path: Path, make_catalog_config: MakeCatalogConfig) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    with pytest.raises(ValueError, match="location_selection references unknown location ids"):
        build_catalog(make_catalog_config(properties_path, location_selection=("missing_location",)))


def test_catalog_rejects_duplicate_config_location_ids(
    tmp_path: Path, make_catalog_config: MakeCatalogConfig, fixture_locations: tuple[LocationConfig, ...]
) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)
    config = make_catalog_config(properties_path).model_copy(
        update={"locations": (fixture_locations[0], fixture_locations[0])}
    )

    with pytest.raises(ValueError, match="duplicate location ids"):
        build_catalog(config)


def test_catalog_rejects_asset_for_unknown_property(tmp_path: Path, make_catalog_config: MakeCatalogConfig) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    with pytest.raises(ValueError, match="property_assets reference unknown property ids"):
        build_catalog(
            make_catalog_config(
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
