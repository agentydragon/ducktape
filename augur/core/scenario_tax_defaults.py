"""Glue between scenario shapes and per-location tax-regime defaults.

`augur.core.local_regulation` owns the regulation data and pure-regulation
logic (`tax_regimes_for_local_regulation` takes bool occupancy/rental signals).
`augur.core.scenario_set` owns the scenario shape. This module is the only
place that imports both — it converts scenario occupancy/rental enums into
the bool signals the regulation API expects, and backfills the scenario's
`property_selection` + `tax_regimes` from the location's modeled defaults.

Keeping this glue out of either side keeps `augur/core` acyclic:
`local_regulation` ← `scenario_set` ← `scenario_tax_defaults`.
"""

from __future__ import annotations

from augur.core.local_regulation import LocalRegulation, tax_regimes_for_local_regulation
from augur.core.scenario_set import OccupancyMode, RentalMode, Scenario


def scenario_with_location_tax_defaults(scenario: Scenario, local_regulation: LocalRegulation) -> Scenario:
    """Backfill `scenario.property_selection.tax_regime`,
    `scenario.property_selection.local_regulation`, and `scenario.tax_regimes`
    from the location's modeled defaults, leaving any caller-supplied values
    untouched.

    A property the owner lives in *and* rents whole is treated as an
    investment property for tax-regime purposes; rooms-rented-while-living
    keeps the owner-occupied treatment.
    """
    owner_occupied = (
        scenario.occupancy_plan.occupancy_mode is OccupancyMode.OWNER_LIVES_IN_PROPERTY
        and scenario.rental_plan.rental_mode is not RentalMode.RENT_WHOLE_PROPERTY
    )
    rented = scenario.rental_plan.rental_mode is not RentalMode.NOT_RENTED
    selection = scenario.property_selection.model_copy(
        update={
            "tax_regime": scenario.property_selection.tax_regime or local_regulation.property_tax_regime,
            "local_regulation": scenario.property_selection.local_regulation or local_regulation,
        }
    )
    tax_regimes = tax_regimes_for_local_regulation(
        local_regulation, existing_tax_regimes=scenario.tax_regimes, owner_occupied=owner_occupied, rented=rented
    )
    return scenario.model_copy(update={"property_selection": selection, "tax_regimes": tax_regimes})
