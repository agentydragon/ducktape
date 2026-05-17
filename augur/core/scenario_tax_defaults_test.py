from __future__ import annotations

import pytest_bazel

from augur.core.local_regulation import LocalRegulation, TaxRegime
from augur.core.scenario_set import (
    Actor,
    ActorRole,
    NotRentedRentalPlan,
    OccupancyMode,
    OccupancyPlan,
    PropertySelection,
    RentalPlan,
    Scenario,
    WholePropertyRentalPlan,
)
from augur.core.scenario_tax_defaults import scenario_with_location_tax_defaults


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
        local_transfer_tax_pct=0,
        special_assessment_annual_usd=0,
        notes="San Francisco secured property-tax default used by the consolidated house model.",
    )


def _bare_scenario(
    *,
    occupancy_mode: OccupancyMode,
    rental_plan: RentalPlan,
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


def test_backfills_property_tax_regime_and_regimes_from_location_defaults() -> None:
    regulation = _san_francisco_regulation()
    scenario = _bare_scenario(occupancy_mode=OccupancyMode.OWNER_LIVES_IN_PROPERTY, rental_plan=NotRentedRentalPlan())

    enriched = scenario_with_location_tax_defaults(scenario, regulation)

    assert enriched.property_selection.tax_regime is TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX
    assert enriched.property_selection.local_regulation == regulation
    assert TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX in enriched.tax_regimes
    assert TaxRegime.PRIMARY_RESIDENCE_EXCLUSION in enriched.tax_regimes


def test_preserves_caller_supplied_tax_regime() -> None:
    regulation = _san_francisco_regulation()
    scenario = _bare_scenario(
        occupancy_mode=OccupancyMode.OWNER_LIVES_IN_PROPERTY,
        rental_plan=NotRentedRentalPlan(),
        tax_regime=TaxRegime.VALLEJO_PROPERTY_TAX,
    )

    enriched = scenario_with_location_tax_defaults(scenario, regulation)

    assert enriched.property_selection.tax_regime is TaxRegime.VALLEJO_PROPERTY_TAX


def test_whole_property_rental_with_owner_residence_treated_as_investment() -> None:
    regulation = _san_francisco_regulation()
    scenario = _bare_scenario(
        occupancy_mode=OccupancyMode.OWNER_LIVES_IN_PROPERTY, rental_plan=WholePropertyRentalPlan(monthly_rent_usd=3000)
    )

    enriched = scenario_with_location_tax_defaults(scenario, regulation)

    assert TaxRegime.CALIFORNIA_INVESTMENT_PROPERTY in enriched.tax_regimes
    assert TaxRegime.PRIMARY_RESIDENCE_EXCLUSION not in enriched.tax_regimes
    assert TaxRegime.RENTAL_DEPRECIATION in enriched.tax_regimes


if __name__ == "__main__":
    pytest_bazel.main()
