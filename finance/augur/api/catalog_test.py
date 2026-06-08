"""Catalog/settings builder tests for public-safe fixture composition."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.api.catalog import build_catalog, build_settings
from finance.augur.api.config import LocationConfig, PropertyAssetConfig
from finance.augur.api.conftest import MakeCatalogConfig
from finance.augur.api.local_regulation import TaxRegime


def test_catalog_locations_default_to_loaded_property_source(
    properties_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    catalog = build_catalog(make_catalog_config(properties_path))

    assert [location.id for location in catalog.locations] == ["location_a", "location_b"]
    assert [property_.id for property_ in catalog.properties] == ["location_a_property", "location_b_property"]


def test_catalog_san_francisco_location_carries_modeled_tax_defaults(
    builtin_properties_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    catalog = build_catalog(make_catalog_config(builtin_properties_path))
    location = one(loc for loc in catalog.locations if loc.id == "san_francisco_ca")

    assert location.label == "San Francisco, CA"
    assert location.city == "San Francisco"
    assert location.local_regulation.property_tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert TaxRegime.SAN_FRANCISCO_TRANSFER_TAX in location.local_regulation.default_tax_regimes


def test_catalog_applies_public_property_asset_urls(
    properties_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
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
        catalog.properties_by_id["location_a_property"].image_url == "https://cdn.example.com/augur/location-a-hero.jpg"
    )
    assert catalog.properties_by_id["location_b_property"].image_url is None


def test_catalog_allows_explicit_public_property_asset_url(
    properties_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
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

    assert catalog.properties_by_id["location_b_property"].image_url == (
        "https://cdn.example.com/augur/location-b-hero.jpg"
    )


def test_catalog_location_selection_filters_properties_and_locations(
    properties_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    catalog = build_catalog(make_catalog_config(properties_path, location_selection=("location_a",)))

    assert [location.id for location in catalog.locations] == ["location_a"]
    assert [property_.id for property_ in catalog.properties] == ["location_a_property"]


def test_settings_carries_sampling_limits(properties_path: Path, make_catalog_config: MakeCatalogConfig) -> None:
    settings = build_settings(make_catalog_config(properties_path))

    assert settings.max_rollout_samples == 128
    assert settings.max_horizon_months == 1200


def test_catalog_rejects_unknown_property_location(
    properties_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    records = json.loads(properties_path.read_text(encoding="utf-8"))
    records[0]["location_id"] = "missing_location"
    properties_path.write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(
        ValueError, match="property 'location_a_property' references unknown location 'missing_location'"
    ):
        build_catalog(make_catalog_config(properties_path))


def test_catalog_rejects_unknown_location_selection(
    properties_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
    with pytest.raises(ValueError, match="location_selection references unknown location ids"):
        build_catalog(make_catalog_config(properties_path, location_selection=("missing_location",)))


def test_catalog_rejects_duplicate_config_location_ids(
    properties_path: Path, make_catalog_config: MakeCatalogConfig, fixture_locations: tuple[LocationConfig, ...]
) -> None:
    config = make_catalog_config(properties_path).model_copy(
        update={"locations": (fixture_locations[0], fixture_locations[0])}
    )

    with pytest.raises(ValueError, match="duplicate location ids"):
        build_catalog(config)


def test_catalog_rejects_asset_for_unknown_property(
    properties_path: Path, make_catalog_config: MakeCatalogConfig
) -> None:
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
