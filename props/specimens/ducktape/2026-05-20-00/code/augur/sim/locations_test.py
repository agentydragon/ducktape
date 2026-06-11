"""Tests for the location YAML loader."""

from __future__ import annotations

import pytest_bazel

from augur.sim.locations import load_location


def test_load_san_francisco() -> None:
    loc = load_location("san_francisco")
    assert loc.location_id == "san_francisco"
    assert loc.jurisdiction_ids == ["federal_us", "california"]
    assert loc.display_name == "San Francisco, CA"
    # The property-tax rate is scaffolding for the housing layer.
    assert loc.annual_property_tax_rate > 0


if __name__ == "__main__":
    pytest_bazel.main()
