"""Where a property is, and the property tax it owes there."""

from __future__ import annotations

from dataclasses import dataclass

from finance.augur.model.series import LocationId


@dataclass(frozen=True, kw_only=True)
class Location:
    """A place a property can be bought in.

    `annual_property_tax_rate_ppb` is the ad-valorem rate on the purchase price;
    `annual_special_assessment` a flat annual special tax (e.g. a Mello-Roos assessment) per parcel.
    """

    location_id: LocationId
    annual_property_tax_rate_ppb: int
    annual_special_assessment: int
