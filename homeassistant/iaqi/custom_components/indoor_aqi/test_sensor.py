"""Tests for the Indoor AQI sensor component."""

from datetime import timedelta

import pytest
import pytest_bazel
from custom_components.indoor_aqi.sensor import IndoorAQISensor, compute_iaqi
from hamcrest import assert_that, close_to, contains_inanyorder, has_entries

from homeassistant.const import STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("co2_value", "pm25_value", "expected_iaqi", "expected_bottleneck"),
    [
        # CO2 is the bottleneck (1500 ppm = IAQI 40, PM25 30 μg/m³ = IAQI ~73)
        ("1500", "30", 40.0, "CO₂: 1500.0 ppm"),
        # CO2 bigger bottleneck than PM2.5 (CO2: IAQI 60, PM2.5: IAQI 60.66)
        ("1000", "51", 60.0, "CO₂: 1000.0 ppm, PM2.5: 51.0 μg/m³"),
    ],
)
async def test_sensor_update(hass, co2_value, pm25_value, expected_iaqi, expected_bottleneck):
    """Test sensor updates with different pollutant values."""
    # Create actual sensor entities with constant values
    hass.states.async_set("sensor.co2", co2_value, {"unit_of_measurement": "ppm"})

    hass.states.async_set("sensor.pm25", pm25_value, {"unit_of_measurement": "μg/m³"})

    # Create our sensor using the real hass instance
    sensor = IndoorAQISensor(
        hass=hass,
        name="Test AQI",
        unique_id="test_aqi",
        sensor_map={"co2": "sensor.co2", "pm25": "sensor.pm25"},
        stale_time=timedelta(hours=1),
    )

    # Update the sensor
    sensor.update()

    # Check the results
    native_value = sensor.native_value
    assert native_value is not None
    assert_that(native_value, close_to(expected_iaqi, 0.1))
    assert_that(
        sensor.extra_state_attributes,
        has_entries(
            bottleneck_string=expected_bottleneck,
            iaqi_co2=compute_iaqi("co2", float(co2_value)),
            iaqi_pm25=compute_iaqi("pm25", float(pm25_value)),
            raw_co2=float(co2_value),
            raw_pm25=float(pm25_value),
        ),
    )


async def test_sensor_error_handling(hass, freezer):
    """Test sensor behavior with invalid, missing or stale data."""
    # Staleness is judged by State.last_updated, so this reading must age before the others are set.
    hass.states.async_set("sensor.stale", "100", {"unit_of_measurement": "ppb"})
    freezer.tick(timedelta(hours=2))

    # Normal sensor
    hass.states.async_set("sensor.co2", "800", {"unit_of_measurement": "ppm"})

    # Unavailable sensor
    hass.states.async_set("sensor.unavailable", STATE_UNAVAILABLE)

    # Non-numeric sensor
    hass.states.async_set("sensor.non_numeric", "not a number")

    # Unknown pollutant type sensor
    hass.states.async_set("sensor.unknown_type", "50", {"unit_of_measurement": "unknown"})

    # Create our sensor
    sensor = IndoorAQISensor(
        hass=hass,
        name="Test AQI",
        unique_id="test_aqi",
        sensor_map={
            "co2": "sensor.co2",
            "pm25": "sensor.missing",
            "voc": "sensor.unavailable",
            "nox": "sensor.stale",
            "o3": "sensor.non_numeric",
            "unknown": "sensor.unknown_type",
        },
        stale_time=timedelta(hours=1),
    )

    sensor.update()

    # Only CO2 at 800 ppm (IAQI 70) counts; the stale NOx reading (100 ppb = IAQI 60) would be the minimum if it did.
    assert_that(sensor.native_value, close_to(70.0, 0.1))

    assert_that(
        sensor.extra_state_attributes["sensor_errors"],
        contains_inanyorder(
            "pm25: no state object", "voc: unavailable", "nox: stale", "o3: not numeric", "unknown: bracket unknown"
        ),
    )


async def test_partial_data_log_on_change(hass, caplog):
    """Test that partial data is logged when the set of sensors with errors changes."""
    # First update - CO2 and PM25 are working, VOC is unavailable
    hass.states.async_set("sensor.co2", "800", {"unit_of_measurement": "ppm"})

    hass.states.async_set("sensor.pm25", "30", {"unit_of_measurement": "μg/m³"})

    hass.states.async_set("sensor.voc", STATE_UNAVAILABLE)

    # Create our sensor
    sensor = IndoorAQISensor(
        hass=hass,
        name="Test AQI",
        unique_id="test_aqi",
        sensor_map={"co2": "sensor.co2", "pm25": "sensor.pm25", "voc": "sensor.voc"},
        stale_time=timedelta(hours=1),
    )

    # First update - VOC unavailable
    sensor.update()
    assert "partial data" in caplog.text
    caplog.clear()

    # Second update - same state, no log expected
    sensor.update()
    assert not caplog.records

    # Third update - PM25 becomes unavailable
    hass.states.async_set("sensor.pm25", STATE_UNAVAILABLE)
    sensor.update()
    assert "Newly unavailable: pm25" in caplog.text
    caplog.clear()

    # Fourth update - PM25 back to normal, VOC still unavailable
    hass.states.async_set("sensor.pm25", "30", {"unit_of_measurement": "μg/m³"})
    sensor.update()
    assert "Newly available: pm25" in caplog.text


async def test_log_after_hour_unchanged(hass, caplog, freezer):
    """Test that partial data is logged again after an hour even if unchanged."""
    # Setup - CO2 working, VOC unavailable
    hass.states.async_set("sensor.co2", "800", {"unit_of_measurement": "ppm"})

    hass.states.async_set("sensor.voc", STATE_UNAVAILABLE)

    # Create our sensor
    sensor = IndoorAQISensor(
        hass=hass,
        name="Test AQI",
        unique_id="test_aqi",
        sensor_map={"co2": "sensor.co2", "voc": "sensor.voc"},
        # Longer than the jump below, so the CO2 reading is not stale by then.
        stale_time=timedelta(hours=2),
    )

    # First update - VOC unavailable
    sensor.update()
    assert "partial data" in caplog.text
    caplog.clear()

    # Second update - same state, no log expected
    sensor.update()
    assert not caplog.records

    # Update again after more than an hour - should log
    freezer.tick(timedelta(hours=1, minutes=1))
    sensor.update()
    assert "partial data" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
