"""Deterministic occupancy-aware pricing functions.

Property costs (insurance, maintenance) scale with how the property is
being used. Landlord-side insurance (DP-3) runs noticeably higher than
owner-occupied (HO-3); rental maintenance runs higher than
owner-occupied because tenants are harder on the property and you
cannot defer fixes the way an owner can.

These are deterministic, state-conditional pricing functions. They
take a base rate (e.g. annual_insurance_pct) plus state (occupancy
mode, rented fraction) and return the applied rate. Pure Python; no
randomness, no sampling — that lives in `augur/model/` when a future
pricing model is stochastic (e.g. mortgage-offer model conditioned on
credit score, sampled from a fitted distribution).

The multipliers are industry-underwriting heuristics. They can be
replaced with fitted models later; the interface stays the same.
"""

from __future__ import annotations

from enum import StrEnum


class OccupancyMode(StrEnum):
    """How the property is being used this month."""

    OFF = "off"
    OWNER_OCCUPIED = "owner_occupied"
    RENTED_PARTIAL = "rented_partial"
    RENTED_FULL = "rented_full"


_LANDLORD_INSURANCE_MULTIPLIER = 1.20
_LANDLORD_MAINTENANCE_MULTIPLIER = 1.50


def insurance_rate(*, base_annual_pct: float, occupancy_mode: OccupancyMode, rented_fraction: float) -> float:
    """Annual insurance rate (pct of property value) for the given occupancy.

    Owner-occupied: base rate (HO-3 policy).
    Rented full: base × landlord multiplier (DP-3 policy).
    Rented partial: linear interpolation by rented_fraction.
    Off (vacant, not rented): base rate; insurer treats vacant similarly to owner-occupied
    for short windows; long-vacancy surcharges are out of scope.
    """

    if occupancy_mode in (OccupancyMode.OWNER_OCCUPIED, OccupancyMode.OFF):
        return base_annual_pct
    if occupancy_mode is OccupancyMode.RENTED_FULL:
        return base_annual_pct * _LANDLORD_INSURANCE_MULTIPLIER
    # RENTED_PARTIAL: interpolate between owner-occupied (1.0) and landlord (multiplier)
    # weighted by the rented fraction.
    blend = 1.0 + (_LANDLORD_INSURANCE_MULTIPLIER - 1.0) * rented_fraction
    return base_annual_pct * blend


def maintenance_rate(*, base_annual_pct: float, occupancy_mode: OccupancyMode, rented_fraction: float) -> float:
    """Annual maintenance rate (pct of property value) for the given occupancy.

    Rental maintenance runs higher than owner-occupied; partial rental
    interpolates by rented fraction.
    """

    if occupancy_mode in (OccupancyMode.OWNER_OCCUPIED, OccupancyMode.OFF):
        return base_annual_pct
    if occupancy_mode is OccupancyMode.RENTED_FULL:
        return base_annual_pct * _LANDLORD_MAINTENANCE_MULTIPLIER
    blend = 1.0 + (_LANDLORD_MAINTENANCE_MULTIPLIER - 1.0) * rented_fraction
    return base_annual_pct * blend
