"""Where a property is, and the property tax it owes there."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator

from finance.augur.model.series import LocationId
from finance.augur.sim.fixed_point import validate_currency_amount
from finance.augur.sim.ids import JurisdictionId

type CurrencyAmount = Annotated[Decimal, BeforeValidator(validate_currency_amount)]


@dataclass(frozen=True, kw_only=True)
class Location:
    """A place a property can be bought in.

    `annual_property_tax_rate_ppb` is the ad-valorem rate on the purchase price;
    `annual_special_assessment` a flat annual special tax (e.g. a Mello-Roos assessment) per parcel.
    """

    location_id: LocationId
    annual_property_tax_rate_ppb: int
    annual_special_assessment: int


class LocationConfig(BaseModel):
    """A location as the files under `data/locations/` spell it.

    `jurisdiction_ids` are the taxing authorities that apply (used by tax
    profiles). `annual_property_tax_rate` is the ad-valorem base + voter-bond
    rate as a fraction of assessed value (e.g. 0.01180 for SF: 1% Prop 13 base
    + ~0.18% city voter-approved bonds). `annual_special_assessment` is a
    flat annual special-tax / CFD (Mello-Roos) assessment in the scenario currency per
    residential parcel.
    """

    location_id: LocationId
    display_name: str
    jurisdiction_ids: list[JurisdictionId]
    annual_property_tax_rate: float
    annual_special_assessment: CurrencyAmount = Decimal(0)
