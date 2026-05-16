from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.core.local_regulation import (
    BUILTIN_LOCATION_CONFIGS,
    LocationId,
    TaxRegime,
    _validate_builtin_location_data,
    _validate_local_regulation_data,
    known_location_id,
    local_regulation_for_location,
    scenario_with_location_tax_defaults,
    tax_regimes_for_scenario,
)
from augur.core.market_bundle_test_support import constant_market_bundle
from augur.core.property_tax import monthly_property_tax_usd
from augur.core.scenario_set import (
    Actor,
    ActorRole,
    NotRentedRentalPlan,
    OccupancyMode,
    OccupancyPlan,
    PropertySelection,
    RentalMode,
    Scenario,
)


def _valid_payload() -> dict[str, dict[str, dict[str, object]]]:
    return {
        "local_regulation_by_location": {
            location_id.value: {
                "property_tax_regime": "san_francisco_secured_property_tax",
                "default_tax_regimes": [
                    "california_prop13",
                    "federal_mortgage_interest",
                    "san_francisco_secured_property_tax",
                ],
                "property_tax_annual_pct": 1.0,
                "local_transfer_tax_pct": 0.0,
                "special_assessment_annual_usd": 0.0,
                "notes": f"{location_id.value} test fixture",
            }
            for location_id in LocationId
        }
    }


def _valid_location_payload() -> dict[str, list[dict[str, object]]]:
    return {
        "builtin_locations": [
            {
                "location_id": location_id.value,
                "label": f"{location_id.value} fixture",
                "city": "Fixture City",
                "state": "CA",
                "notes": [f"{location_id.value} fixture note"],
            }
            for location_id in LocationId
        ]
    }


def test_unknown_location_rejected() -> None:
    assert known_location_id("oakland_ca") is None
    with pytest.raises(ValueError, match="unknown built-in local regulation"):
        local_regulation_for_location("oakland_ca")


def test_builtin_location_configs_cover_local_regulation_locations() -> None:
    location_ids = tuple(location.location_id for location in BUILTIN_LOCATION_CONFIGS)

    assert set(location_ids) == set(LocationId)
    assert len(location_ids) == len(set(location_ids))
    for location in BUILTIN_LOCATION_CONFIGS:
        regulation = local_regulation_for_location(location.location_id)
        assert regulation.property_tax_regime in regulation.default_tax_regimes


def test_loaded_builtin_regulation_drives_property_tax() -> None:
    regulation = local_regulation_for_location(LocationId.MARE_ISLAND_VALLEJO_CA)

    taxes = monthly_property_tax_usd(
        purchase_price_usd=100_000,
        local_regulation=regulation,
        market_bundle=constant_market_bundle(inflation_path=(1.0, 1.0)),
    )

    np.testing.assert_allclose(taxes[:, 0], 0.0)
    np.testing.assert_allclose(taxes[:, 1], 100_000 * 0.024 / 12)


def test_loaded_builtin_regulation_drives_tax_regime_defaults() -> None:
    regulation = local_regulation_for_location(LocationId.SAN_FRANCISCO_CA)

    regimes = tax_regimes_for_scenario(
        regulation,
        occupancy_mode=OccupancyMode.OWNER_LIVES_IN_PROPERTY,
        rental_mode=RentalMode.RENT_ROOMS_WHILE_OWNER_LIVES_THERE,
    )

    assert regulation.property_tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert TaxRegime.SAN_FRANCISCO_TRANSFER_TAX in regimes
    assert TaxRegime.PRIMARY_RESIDENCE_EXCLUSION in regimes
    assert TaxRegime.RENTAL_DEPRECIATION in regimes
    assert TaxRegime.CALIFORNIA_OWNER_OCCUPIED in regimes


def test_whole_property_rental_with_owner_residence_treated_as_investment() -> None:
    regulation = local_regulation_for_location(LocationId.SAN_FRANCISCO_CA)

    regimes = tax_regimes_for_scenario(
        regulation, occupancy_mode=OccupancyMode.OWNER_LIVES_IN_PROPERTY, rental_mode=RentalMode.RENT_WHOLE_PROPERTY
    )

    assert TaxRegime.CALIFORNIA_INVESTMENT_PROPERTY in regimes
    assert TaxRegime.PRIMARY_RESIDENCE_EXCLUSION not in regimes
    assert TaxRegime.RENTAL_DEPRECIATION in regimes


def test_existing_tax_regimes_are_preserved_and_deduplicated() -> None:
    regulation = local_regulation_for_location(LocationId.SAN_FRANCISCO_CA)

    regimes = tax_regimes_for_scenario(
        regulation,
        occupancy_mode=OccupancyMode.OWNER_LIVES_IN_PROPERTY,
        rental_mode=RentalMode.NOT_RENTED,
        existing_tax_regimes=(TaxRegime.CALIFORNIA_PROP13, TaxRegime.MARE_ISLAND_SPECIAL_ASSESSMENTS),
    )

    assert TaxRegime.MARE_ISLAND_SPECIAL_ASSESSMENTS in regimes
    assert regimes.count(TaxRegime.CALIFORNIA_PROP13) == 1


def _bare_scenario(
    *,
    occupancy_mode: OccupancyMode,
    rental_plan: NotRentedRentalPlan,
    tax_regime: TaxRegime | None = None,
    existing_tax_regimes: tuple[TaxRegime, ...] = (),
) -> Scenario:
    return Scenario(
        scenario_id="fixture",
        label="Fixture",
        actors=(Actor(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER),),
        property_selection=PropertySelection(tax_regime=tax_regime),
        occupancy_plan=OccupancyPlan(occupancy_mode=occupancy_mode),
        rental_plan=rental_plan,
        tax_regimes=existing_tax_regimes,
    )


def test_scenario_with_location_tax_defaults_backfills_property_tax_regime() -> None:
    regulation = local_regulation_for_location(LocationId.SAN_FRANCISCO_CA)
    scenario = _bare_scenario(occupancy_mode=OccupancyMode.OWNER_LIVES_IN_PROPERTY, rental_plan=NotRentedRentalPlan())

    enriched = scenario_with_location_tax_defaults(scenario, regulation)

    assert enriched.property_selection.tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert enriched.property_selection.local_regulation == regulation
    assert TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX in enriched.tax_regimes
    assert TaxRegime.PRIMARY_RESIDENCE_EXCLUSION in enriched.tax_regimes


def test_scenario_with_location_tax_defaults_preserves_caller_overrides() -> None:
    regulation = local_regulation_for_location(LocationId.SAN_FRANCISCO_CA)
    scenario = _bare_scenario(
        occupancy_mode=OccupancyMode.OWNER_LIVES_IN_PROPERTY,
        rental_plan=NotRentedRentalPlan(),
        tax_regime=TaxRegime.VALLEJO_PROPERTY_TAX,
    )

    enriched = scenario_with_location_tax_defaults(scenario, regulation)

    assert enriched.property_selection.tax_regime is TaxRegime.VALLEJO_PROPERTY_TAX


def test_local_regulation_data_requires_all_builtin_locations() -> None:
    payload = _valid_payload()
    table = dict(payload["local_regulation_by_location"])
    del table[LocationId.MARE_ISLAND_VALLEJO_CA.value]
    payload["local_regulation_by_location"] = table

    with pytest.raises(ValidationError, match="missing: mare_island_vallejo_ca"):
        _validate_local_regulation_data(payload)


def test_builtin_location_data_requires_all_builtin_locations() -> None:
    payload = _valid_location_payload()
    payload["builtin_locations"] = [
        location for location in payload["builtin_locations"] if location["location_id"] != "mare_island_vallejo_ca"
    ]

    with pytest.raises(ValidationError, match="missing: mare_island_vallejo_ca"):
        _validate_builtin_location_data(payload)


def test_builtin_location_data_rejects_duplicate_locations() -> None:
    payload = _valid_location_payload()
    payload["builtin_locations"].append(dict(payload["builtin_locations"][0]))

    with pytest.raises(ValidationError, match="duplicate location ids"):
        _validate_builtin_location_data(payload)


def test_local_regulation_data_requires_regulation_fields() -> None:
    payload = _valid_payload()
    table = dict(payload["local_regulation_by_location"])
    san_francisco = dict(table[LocationId.SAN_FRANCISCO_CA.value])
    del san_francisco["notes"]
    table[LocationId.SAN_FRANCISCO_CA.value] = san_francisco
    payload["local_regulation_by_location"] = table

    with pytest.raises(ValidationError, match="notes"):
        _validate_local_regulation_data(payload)


def test_local_regulation_data_requires_tax_regime_fields() -> None:
    payload = _valid_payload()
    table = dict(payload["local_regulation_by_location"])
    san_francisco = dict(table[LocationId.SAN_FRANCISCO_CA.value])
    del san_francisco["property_tax_regime"]
    table[LocationId.SAN_FRANCISCO_CA.value] = san_francisco
    payload["local_regulation_by_location"] = table

    with pytest.raises(ValidationError, match="property_tax_regime"):
        _validate_local_regulation_data(payload)


if __name__ == "__main__":
    pytest_bazel.main()
