"""Location records loaded from YAML.

A `Location` is a place an agent can reside in — at spike 1 only
"san_francisco" is shipped. Locations carry the tax jurisdictions
that apply at that address and (for the deferred housing layer)
property-tax / special-assessment rates.

The data files live in `augur/sim/data/locations/*.yaml`; layout
mirrors `jurisdictions/`."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

_DATA_DIR = Path(__file__).parent / "data" / "locations"


class Location(BaseModel):
    """A residence location with tax + housing-cost configuration.
    `jurisdiction_ids` are the taxing authorities that apply (used
    by tax profiles); the property-tax fields are scaffolding for
    the deferred housing layer."""

    location_id: str
    display_name: str
    jurisdiction_ids: list[str]
    annual_property_tax_rate: float = 0.0
    annual_special_assessment_rate: float = 0.0


def load_location(location_id: str) -> Location:
    path = _DATA_DIR / f"{location_id}.yaml"
    return Location.model_validate(yaml.safe_load(path.read_text()))
