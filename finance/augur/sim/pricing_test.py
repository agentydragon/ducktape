"""Tests for occupancy-aware pricing functions."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_bazel

from finance.augur.sim.pricing import OccupancyMode, insurance_rate, maintenance_rate


@pytest.mark.parametrize(("rate", "base"), [(insurance_rate, 0.4), (maintenance_rate, 1.0)])
def test_occupancy_rates_preserve_base_and_interpolate(rate: Callable[..., float], base: float) -> None:
    owner = rate(base_annual_pct=base, occupancy_mode=OccupancyMode.OWNER_OCCUPIED, rented_fraction=0.0)
    landlord = rate(base_annual_pct=base, occupancy_mode=OccupancyMode.RENTED_FULL, rented_fraction=1.0)
    half = rate(base_annual_pct=base, occupancy_mode=OccupancyMode.RENTED_PARTIAL, rented_fraction=0.5)
    assert owner == base
    assert owner < half < landlord
    assert half == pytest.approx((owner + landlord) / 2)


def test_vacant_insurance_returns_base() -> None:
    assert insurance_rate(base_annual_pct=0.4, occupancy_mode=OccupancyMode.OFF, rented_fraction=0.0) == 0.4


if __name__ == "__main__":
    pytest_bazel.main()
