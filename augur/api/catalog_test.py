"""Bootstrap catalog tests for public-safe fixture composition."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_bazel

from augur.api.backend import AugurBackend, AugurBackendRuntimeConfig
from augur.api.catalog import build_bootstrap_payload
from augur.api.config import (
    AgentDefinition,
    AugurConfig,
    ConcentratedHoldingSnapshot,
    FinanceSnapshot,
    LocationConfig,
    PersonalFinanceConfig,
    PropertyAssetConfig,
    PropertySourceConfig,
)
from augur.core.local_regulation import LocalRegulation
from augur.core.market_bundle import SimpleMarketBundleProvider
from augur.core.scenario_set import ActorRole, ScenarioSet, TaxRegime


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
    asset_base_url: str | None = None,
    property_assets: tuple[PropertyAssetConfig, ...] = (),
) -> AugurConfig:
    return AugurConfig(
        agents=(AgentDefinition(actor_id="agent_a", label="Agent A", role=ActorRole.PRIMARY_OWNER),),
        personal_finance=PersonalFinanceConfig(minimum_liquid_reserve_usd=0),
        property_source=PropertySourceConfig(
            properties_path=properties_path, asset_base_url=asset_base_url, property_assets=property_assets
        ),
        snapshot=FinanceSnapshot(
            as_of_date="2026-05-14",
            cash_usd=12_345,
            wealthfront_sp500_usd=61_000,
            ibkr_vt_usd=39_000,
            sp500_proxy_portfolio_usd=100_000,
            concentrated_holdings=(
                ConcentratedHoldingSnapshot(
                    holding_id="fixture_holding_a",
                    label="Fixture Holding A",
                    units=500,
                    fmv_usd_per_unit=20,
                    basis_per_unit_usd=5,
                    valuation_source="fixture mark",
                ),
            ),
        ),
        starting_portfolio_usd=100_000,
        locations=_fixture_locations(),
        location_selection=location_selection,
    )


def _property_by_id(bootstrap, property_id: str):
    return next(property_ for property_ in bootstrap.properties if property_.id == property_id)


def test_bootstrap_locations_default_to_loaded_property_source(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    bootstrap = build_bootstrap_payload(_config(properties_path))

    assert [location.id for location in bootstrap.locations] == ["location_a", "location_b"]
    assert [property_.id for property_ in bootstrap.properties] == ["location_a_property", "location_b_property"]


def test_bootstrap_san_francisco_location_carries_modeled_tax_defaults(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_builtin_properties(properties_path)

    bootstrap = build_bootstrap_payload(_config(properties_path))
    location = next(loc for loc in bootstrap.locations if loc.id == "san_francisco_ca")

    assert location.label == "San Francisco, CA"
    assert location.city == "San Francisco"
    assert location.local_regulation.property_tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert TaxRegime.SAN_FRANCISCO_TRANSFER_TAX in location.local_regulation.default_tax_regimes


def test_backend_applies_location_tax_defaults_to_scenario(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_builtin_properties(properties_path)
    backend = AugurBackend(
        augur_config=_config(properties_path),
        runtime_config=AugurBackendRuntimeConfig(
            market_bundle_provider=SimpleMarketBundleProvider(), default_rollout_samples=8, max_rollout_samples=128
        ),
    )
    request = {
        "scenario_set_id": "tax_defaults",
        "title": "Tax defaults",
        "market_request": {"rollout_count": 1, "horizon_months": 1, "seed": 1},
        "scenarios": [
            {
                "scenario_id": "sf_property",
                "label": "SF Property",
                "actors": [{"actor_id": "agent_a", "label": "Agent A", "role": "primary_owner"}],
                "property_selection": {"property_id": "sf_property"},
                "occupancy_plan": {"occupancy_mode": "owner_lives_in_property"},
                "rental_plan": {"rental_mode": "not_rented"},
            }
        ],
    }

    scenario_set = backend._scenario_set_with_catalog_defaults(ScenarioSet.model_validate(request))
    scenario = scenario_set.scenarios[0]

    assert scenario.property_selection.tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX in scenario.tax_regimes
    assert TaxRegime.SAN_FRANCISCO_TRANSFER_TAX in scenario.tax_regimes
    assert TaxRegime.PRIMARY_RESIDENCE_EXCLUSION in scenario.tax_regimes


def test_bootstrap_applies_public_property_asset_base_url(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    bootstrap = build_bootstrap_payload(
        _config(
            properties_path,
            asset_base_url="https://assets.example.com/augur/property-images",
            property_assets=(PropertyAssetConfig(property_id="location_a_property", asset_id="location_a_hero"),),
        )
    )

    assert (
        _property_by_id(bootstrap, "location_a_property").image_url
        == "https://assets.example.com/augur/property-images/location_a_hero"
    )
    assert _property_by_id(bootstrap, "location_b_property").image_url is None


def test_bootstrap_allows_explicit_public_property_asset_url(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    bootstrap = build_bootstrap_payload(
        _config(
            properties_path,
            property_assets=(
                PropertyAssetConfig(
                    property_id="location_b_property",
                    asset_id="location_b_hero",
                    image_url="https://cdn.example.com/augur/location-b-hero.jpg",
                ),
            ),
        )
    )

    assert _property_by_id(bootstrap, "location_b_property").image_url == (
        "https://cdn.example.com/augur/location-b-hero.jpg"
    )


def test_bootstrap_location_selection_filters_properties_and_locations(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    bootstrap = build_bootstrap_payload(_config(properties_path, location_selection=("location_a",)))

    assert [location.id for location in bootstrap.locations] == ["location_a"]
    assert [property_.id for property_ in bootstrap.properties] == ["location_a_property"]


def test_bootstrap_carries_configured_finance_snapshot_and_defaults(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    bootstrap = build_bootstrap_payload(_config(properties_path))

    assert bootstrap.default_initial_checking_usd == 12_345
    assert bootstrap.default_knobs.starting_portfolio_usd == 100_000
    assert bootstrap.finance_snapshot.cash_usd == 12_345
    assert bootstrap.finance_snapshot.wealthfront_sp500_usd == 61_000
    assert bootstrap.finance_snapshot.ibkr_vt_usd == 39_000
    assert bootstrap.finance_snapshot.sp500_proxy_portfolio_usd == 100_000
    assert bootstrap.finance_snapshot.concentrated_holdings[0].label == "Fixture Holding A"
    assert bootstrap.finance_snapshot.concentrated_holdings[0].units == 500
    assert bootstrap.finance_snapshot.concentrated_holdings[0].value_usd == 10_000


def test_bootstrap_rejects_unknown_property_location(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)
    records = json.loads(properties_path.read_text(encoding="utf-8"))
    records[0]["location_id"] = "missing_location"
    properties_path.write_text(json.dumps(records), encoding="utf-8")

    with pytest.raises(
        ValueError, match="property 'location_a_property' references unknown location 'missing_location'"
    ):
        build_bootstrap_payload(_config(properties_path))


def test_bootstrap_rejects_unknown_location_selection(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    with pytest.raises(ValueError, match="location_selection references unknown location ids"):
        build_bootstrap_payload(_config(properties_path, location_selection=("missing_location",)))


def test_bootstrap_rejects_duplicate_config_location_ids(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)
    config = _config(properties_path).model_copy(
        update={"locations": (_fixture_locations()[0], _fixture_locations()[0])}
    )

    with pytest.raises(ValueError, match="duplicate location ids"):
        build_bootstrap_payload(config)


def test_bootstrap_rejects_asset_for_unknown_property(tmp_path: Path) -> None:
    properties_path = tmp_path / "properties.json"
    _write_properties(properties_path)

    with pytest.raises(ValueError, match="property_assets reference unknown property ids"):
        build_bootstrap_payload(
            _config(
                properties_path,
                asset_base_url="https://assets.example.com/augur/property-images",
                property_assets=(
                    PropertyAssetConfig(property_id="missing_property", asset_id="missing_property_hero"),
                ),
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
