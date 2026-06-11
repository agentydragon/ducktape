"""Tests for occupancy-aware pricing functions."""

from __future__ import annotations

import pytest
import pytest_bazel

from finance.augur.sim.pricing import OccupancyMode, insurance_rate, maintenance_rate


class TestInsuranceRate:
    def test_owner_occupied_returns_base(self):
        assert (
            insurance_rate(base_annual_pct=0.4, occupancy_mode=OccupancyMode.OWNER_OCCUPIED, rented_fraction=0.0) == 0.4
        )

    def test_off_returns_base(self):
        assert insurance_rate(base_annual_pct=0.4, occupancy_mode=OccupancyMode.OFF, rented_fraction=0.0) == 0.4

    def test_rented_full_scales_up_for_landlord(self):
        rate = insurance_rate(base_annual_pct=0.4, occupancy_mode=OccupancyMode.RENTED_FULL, rented_fraction=1.0)
        assert rate > 0.4
        assert rate == pytest.approx(0.4 * 1.20)

    def test_rented_partial_interpolates_by_fraction(self):
        owner = insurance_rate(base_annual_pct=0.4, occupancy_mode=OccupancyMode.OWNER_OCCUPIED, rented_fraction=0.0)
        landlord = insurance_rate(base_annual_pct=0.4, occupancy_mode=OccupancyMode.RENTED_FULL, rented_fraction=1.0)
        half = insurance_rate(base_annual_pct=0.4, occupancy_mode=OccupancyMode.RENTED_PARTIAL, rented_fraction=0.5)
        assert owner < half < landlord
        assert half == pytest.approx((owner + landlord) / 2)


class TestMaintenanceRate:
    def test_owner_occupied_returns_base(self):
        assert (
            maintenance_rate(base_annual_pct=1.0, occupancy_mode=OccupancyMode.OWNER_OCCUPIED, rented_fraction=0.0)
            == 1.0
        )

    def test_rented_full_scales_up_for_landlord(self):
        rate = maintenance_rate(base_annual_pct=1.0, occupancy_mode=OccupancyMode.RENTED_FULL, rented_fraction=1.0)
        assert rate > 1.0
        assert rate == pytest.approx(1.0 * 1.50)

    def test_rented_partial_interpolates_by_fraction(self):
        owner = maintenance_rate(base_annual_pct=1.0, occupancy_mode=OccupancyMode.OWNER_OCCUPIED, rented_fraction=0.0)
        landlord = maintenance_rate(base_annual_pct=1.0, occupancy_mode=OccupancyMode.RENTED_FULL, rented_fraction=1.0)
        half = maintenance_rate(base_annual_pct=1.0, occupancy_mode=OccupancyMode.RENTED_PARTIAL, rented_fraction=0.5)
        assert owner < half < landlord
        assert half == pytest.approx((owner + landlord) / 2)


if __name__ == "__main__":
    pytest_bazel.main()
