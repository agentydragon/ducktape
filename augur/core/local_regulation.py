from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import Field, NonNegativeFloat, model_validator

from augur.core.schemas import ApiModel, Percentage

if TYPE_CHECKING:
    # scenario_set imports LocalRegulation/TaxRegime from this module, so the
    # OccupancyMode/RentalMode imports below have to stay TYPE_CHECKING-only at
    # module load time and lazy at runtime.
    from augur.core.scenario_set import OccupancyMode, RentalMode, Scenario

_LOCAL_REGULATION_DATA_PATH = Path(__file__).with_name("local_regulation.yaml")
_BUILTIN_LOCATION_DATA_PATH = Path(__file__).with_name("builtin_locations.yaml")


class LocationId(StrEnum):
    SAN_FRANCISCO_CA = "san_francisco_ca"
    VALLEJO_CA = "vallejo_ca"
    MARE_ISLAND_VALLEJO_CA = "mare_island_vallejo_ca"


class TaxRegime(StrEnum):
    CALIFORNIA_PROP13 = "california_prop13"
    CALIFORNIA_OWNER_OCCUPIED = "california_owner_occupied"
    CALIFORNIA_INVESTMENT_PROPERTY = "california_investment_property"
    SAN_FRANCISCO_SECURED_PROPERTY_TAX = "san_francisco_secured_property_tax"
    SAN_FRANCISCO_TRANSFER_TAX = "san_francisco_transfer_tax"
    VALLEJO_PROPERTY_TAX = "vallejo_property_tax"
    MARE_ISLAND_SPECIAL_ASSESSMENTS = "mare_island_special_assessments"
    CALIFORNIA_TRANSFER_TAX = "california_transfer_tax"
    FEDERAL_MORTGAGE_INTEREST = "federal_mortgage_interest"
    RENTAL_DEPRECIATION = "rental_depreciation"
    DEPRECIATION_RECAPTURE = "depreciation_recapture"
    FEDERAL_CAPITAL_GAINS = "federal_capital_gains"
    CALIFORNIA_INCOME_TAX = "california_income_tax"
    PRIMARY_RESIDENCE_EXCLUSION = "primary_residence_exclusion"


class LocalRegulation(ApiModel):
    property_tax_regime: TaxRegime = Field(
        description="Primary property-tax regime to put on a selected property for this location."
    )
    default_tax_regimes: tuple[TaxRegime, ...] = Field(
        min_length=1, description="Tax regimes enabled by default for scenarios in this location."
    )
    property_tax_annual_pct: Percentage = Field(
        description="Annual ad-valorem property-tax rate applied to the Prop 13 assessed value."
    )
    local_transfer_tax_pct: Percentage = Field(
        default=0, description="Local transfer-tax rate applied when the property is sold."
    )
    special_assessment_annual_usd: NonNegativeFloat = Field(
        default=0, description="Fixed annual local special assessment added to property-tax cash flow."
    )
    notes: str = Field(description="Human-readable source and modeling notes for this location.")

    @model_validator(mode="after")
    def _validate_tax_regime_defaults(self) -> LocalRegulation:
        duplicates = sorted(
            {regime for regime in self.default_tax_regimes if self.default_tax_regimes.count(regime) > 1}
        )
        if duplicates:
            raise ValueError(f"default_tax_regimes must not contain duplicates: {duplicates}")
        if self.property_tax_regime not in self.default_tax_regimes:
            raise ValueError("default_tax_regimes must include property_tax_regime")
        return self


class BuiltinLocationConfig(ApiModel):
    location_id: LocationId
    label: str
    city: str
    state: str
    notes: tuple[str, ...] = ()


class _LocalRegulationData(ApiModel):
    local_regulation_by_location: dict[LocationId, LocalRegulation]

    @model_validator(mode="after")
    def _validate_complete_location_table(self) -> _LocalRegulationData:
        expected = set(LocationId)
        actual = set(self.local_regulation_by_location)
        if actual != expected:
            missing = ", ".join(location_id.value for location_id in LocationId if location_id not in actual) or "none"
            unexpected = ", ".join(sorted(location_id.value for location_id in actual - expected)) or "none"
            expected_list = ", ".join(location_id.value for location_id in LocationId)
            raise ValueError(
                "local_regulation_by_location must define exactly these locations: "
                f"{expected_list}; missing: {missing}; unexpected: {unexpected}"
            )
        return self


class _BuiltinLocationData(ApiModel):
    builtin_locations: tuple[BuiltinLocationConfig, ...]

    @model_validator(mode="after")
    def _validate_complete_location_table(self) -> _BuiltinLocationData:
        location_ids = tuple(location.location_id for location in self.builtin_locations)
        duplicates = sorted({location_id.value for location_id in location_ids if location_ids.count(location_id) > 1})
        if duplicates:
            raise ValueError(f"builtin_locations must not contain duplicate location ids: {duplicates}")
        expected = set(LocationId)
        actual = set(location_ids)
        if actual != expected:
            missing = ", ".join(location_id.value for location_id in LocationId if location_id not in actual) or "none"
            unexpected = ", ".join(sorted(location_id.value for location_id in actual - expected)) or "none"
            expected_list = ", ".join(location_id.value for location_id in LocationId)
            raise ValueError(
                "builtin_locations must define exactly these locations: "
                f"{expected_list}; missing: {missing}; unexpected: {unexpected}"
            )
        return self


def _validate_local_regulation_data(payload: Any) -> _LocalRegulationData:
    return _LocalRegulationData.model_validate(payload)


def _validate_builtin_location_data(payload: Any) -> _BuiltinLocationData:
    return _BuiltinLocationData.model_validate(payload)


def _load_local_regulation_data(path: Path = _LOCAL_REGULATION_DATA_PATH) -> _LocalRegulationData:
    return _validate_local_regulation_data(yaml.safe_load(path.read_text(encoding="utf-8")))


def _load_builtin_location_data(path: Path = _BUILTIN_LOCATION_DATA_PATH) -> _BuiltinLocationData:
    return _validate_builtin_location_data(yaml.safe_load(path.read_text(encoding="utf-8")))


LOCAL_REGULATION_BY_LOCATION: dict[LocationId, LocalRegulation] = dict(
    _load_local_regulation_data().local_regulation_by_location
)
BUILTIN_LOCATION_CONFIGS: tuple[BuiltinLocationConfig, ...] = _load_builtin_location_data().builtin_locations


def known_location_id(location_id: LocationId | str) -> LocationId | None:
    if isinstance(location_id, LocationId):
        return location_id
    try:
        return LocationId(str(location_id))
    except ValueError:
        return None


def local_regulation_for_location(location_id: LocationId | str) -> LocalRegulation:
    known_id = known_location_id(location_id)
    if known_id is None:
        raise ValueError(f"unknown built-in local regulation for location {location_id!r}")
    return LOCAL_REGULATION_BY_LOCATION[known_id]


def tax_regimes_for_scenario(
    local_regulation: LocalRegulation,
    *,
    occupancy_mode: OccupancyMode,
    rental_mode: RentalMode,
    existing_tax_regimes: tuple[TaxRegime, ...] = (),
) -> tuple[TaxRegime, ...]:
    """Combine a location's modeled tax-regime defaults with scenario-level
    owner-occupancy and rental signals.

    A property the owner lives in *and* rents whole is treated as an
    investment property for tax-regime purposes; rooms-rented-while-living
    keeps the owner-occupied treatment."""
    # scenario_set imports LocalRegulation/TaxRegime from this module, so
    # OccupancyMode/RentalMode have to be resolved lazily here.
    from augur.core.scenario_set import OccupancyMode, RentalMode  # noqa: PLC0415

    owner_occupied = (
        occupancy_mode is OccupancyMode.OWNER_LIVES_IN_PROPERTY and rental_mode is not RentalMode.RENT_WHOLE_PROPERTY
    )
    rented = rental_mode is not RentalMode.NOT_RENTED
    regimes = [
        *existing_tax_regimes,
        *local_regulation.default_tax_regimes,
        TaxRegime.CALIFORNIA_OWNER_OCCUPIED if owner_occupied else TaxRegime.CALIFORNIA_INVESTMENT_PROPERTY,
    ]
    if owner_occupied:
        regimes.append(TaxRegime.PRIMARY_RESIDENCE_EXCLUSION)
    if rented:
        regimes.append(TaxRegime.RENTAL_DEPRECIATION)
        regimes.append(TaxRegime.DEPRECIATION_RECAPTURE)
    return tuple(dict.fromkeys(regimes))


def scenario_with_location_tax_defaults(scenario: Scenario, local_regulation: LocalRegulation) -> Scenario:
    """Backfill a scenario's `property_selection.tax_regime` and `tax_regimes`
    from the location's modeled defaults, leaving any caller-supplied values
    untouched."""
    selection = scenario.property_selection.model_copy(
        update={
            "tax_regime": scenario.property_selection.tax_regime or local_regulation.property_tax_regime,
            "local_regulation": scenario.property_selection.local_regulation or local_regulation,
        }
    )
    tax_regimes = tax_regimes_for_scenario(
        local_regulation,
        occupancy_mode=scenario.occupancy_plan.occupancy_mode,
        rental_mode=scenario.rental_plan.rental_mode,
        existing_tax_regimes=scenario.tax_regimes,
    )
    return scenario.model_copy(update={"property_selection": selection, "tax_regimes": tax_regimes})
