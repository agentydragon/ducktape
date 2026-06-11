"""Basic tests for the Indoor AQI sensor component."""

import pytest_bazel
from custom_components.indoor_aqi.sensor import compute_iaqi
from hamcrest import assert_that, close_to


def test_compute_iaqi_normal_values():
    """Test IAQI calculation with normal values within ranges."""
    assert compute_iaqi("co2", 400) == 100  # Best value
    assert compute_iaqi("co2", 1500) == 40  # Mid-range value
    assert compute_iaqi("co2", 4000) == 0  # Worst value


def test_compute_iaqi_clamping():
    """Test IAQI calculation with values outside the range (should be clamped)."""
    assert compute_iaqi("co2", 300) == 100  # Below min
    assert compute_iaqi("co2", 5000) == 0  # Above max


def test_compute_iaqi_interpolation():
    """Test IAQI calculation with interpolation."""
    # At 800 ppm CO2, we should be between (600, 80) and (1000, 60)
    # Linear interpolation: 80 - (800-600)/(1000-600)*(80-60) = 80 - 0.5*20 = 80 - 10 = 70
    result = compute_iaqi("co2", 800)
    assert result is not None
    assert_that(result, close_to(70.0, 0.01))


def test_compute_iaqi_unknown_pollutant():
    """Test IAQI calculation with unknown pollutant."""
    assert compute_iaqi("unknown", 100) is None


if __name__ == "__main__":
    pytest_bazel.main()
