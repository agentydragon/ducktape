"""Tests for the Location model and YAML fixture data."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
import yaml

from finance.augur.sim.locations import Location

_DATA_DIR = Path(__file__).parent / "data" / "locations"


def _load_location(location_id: str) -> Location:
    path = _DATA_DIR / f"{location_id}.yaml"
    return Location.model_validate(yaml.safe_load(path.read_text()))


def test_load_san_francisco() -> None:
    loc = _load_location("san_francisco")
    assert loc.location_id == "san_francisco"
    assert loc.jurisdiction_ids == ["federal_us", "california"]
    assert loc.display_name == "San Francisco, CA"
    # The property-tax rate is scaffolding for the housing layer.
    assert loc.annual_property_tax_rate > 0


if __name__ == "__main__":
    pytest_bazel.main()
