"""Location records for the simulation engine.

A `Location` is a place an agent can reside in. Locations carry the tax
jurisdictions that apply at that address and property-tax / special-assessment
rates used by the compiler to build per-property cash-flow arrays.
"""

from __future__ import annotations

from pydantic import BaseModel


class Location(BaseModel):
    """A residence location with tax + housing-cost configuration.

    `jurisdiction_ids` are the taxing authorities that apply (used by tax
    profiles). `annual_property_tax_rate` is the ad-valorem base + voter-bond
    rate as a fraction of assessed value (e.g. 0.01180 for SF: 1% Prop 13 base
    + ~0.18% city voter-approved bonds). `annual_special_assessment_usd` is a
    flat annual special-tax / CFD (Mello-Roos) assessment in dollars per
    residential parcel.
    """

    location_id: str
    display_name: str
    jurisdiction_ids: list[str]
    annual_property_tax_rate: float
    annual_special_assessment_usd: float = 0.0
