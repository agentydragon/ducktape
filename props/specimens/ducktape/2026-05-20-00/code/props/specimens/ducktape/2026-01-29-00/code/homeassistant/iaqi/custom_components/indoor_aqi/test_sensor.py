"""Tests for the Indoor AQI sensor component."""

import logging
from datetime import UTC, datetime, timedelta
from logging.handlers import MemoryHandler

import pytest
import pytest_bazel
from custom_components.indoor_aqi.sensor import _LOGGER, IndoorAQISensor, compute_iaqi
from hamcrest import assert_that, close_to, contains_inanyorder, has_entries
from hamcrest.core.base_matcher import BaseMatcher

from homeassistant.const import STATE_UNAVAILABLE


class LogEntryContaining(BaseMatcher):
    """Matcher for log entries containing specific text."""

    def __init__(self, text):
        self.text = text

    def _matches(self, item):
        if not item:
            return False
        if not hasattr(item[0], "getMessage"):
            return False
        return self.text in item[0].getMessage()

    def describe_to(self, description):
        description.append_text(f'a log entry containing "{self.text}"')

    def describe_mismatch(self, item, mismatch_description):
        if not item:
            mismatch_description.append_text("was empty log buffer")
        elif not hasattr(item[0], "getMessage"):
            mismatch_description.append_text("was not a log entry")
        else:
            mismatch_description.append_text(f'was "{item[0].getMessage()}"')


class EmptyLogBuffer(BaseMatcher):
    """Matcher for empty log buffer."""

    def _matches(self, item):
        return not item

    def describe_to(self, description):
        description.append_text("an empty log buffer")

    def describe_mismatch(self, item, mismatch_description):
        if item:
            mismatch_description.append_text(f"had {len(item)} entries")


def log_containing(text):
    """Returns a matcher for log entries containing the specified text."""
    return LogEntryContaining(text)


def empty_log():
    """Returns a matcher for empty log buffer."""
    return EmptyLogBuffer()


@pytest.fixture
def now():
    return datetime.now(UTC)


@pytest.fixture
def memory_handler():
    """Create a memory handler to capture log messages."""
    # Create a memory handler that stores log records in memory
    handler = MemoryHandler(capacity=100)  # Store up to 100 log records

    # Save the original handlers
    original_handlers = _LOGGER.handlers.copy()
    original_level = _LOGGER.level

    # Configure the logger to use our memory handler and ensure WARNING level is enabled
    _LOGGER.setLevel(logging.WARNING)
    _LOGGER.handlers = [handler]

    yield handler

    # Clean up: restore original handlers and level
    _LOGGER.handlers = original_handlers
    _LOGGER.setLevel(original_level)


@pytest.mark.parametrize(
    ("co2_value", "pm25_value", "expected_iaqi", "expected_bottleneck"),
    [
        # CO2 is the bottleneck (1500 ppm = IAQI 40, PM25 30 μg/m³ = IAQI ~73)
        ("1500", "30", 40.0, "CO₂: 1500.0 ppm"),
        # CO2 bigger bottleneck than PM2.5 (CO2: IAQI 60, PM2.5: IAQI 60.66)
        ("1000", "51", 60.0, "CO₂: 1000.0 ppm, PM2.5: 51.0 μg/m³"),
    ],
)
async def test_sensor_update(hass, co2_value, pm25_value, expected_iaqi, expected_bottleneck, now):
    """Test sensor updates with different pollutant values."""
    # Create actual sensor entities with constant values
    hass.states.async_set("sensor.co2", co2_value, {"unit_of_measurement": "ppm", "last_updated": now})

    hass.states.async_set("sensor.pm25", pm25_value, {"unit_of_measurement": "μg/m³", "last_updated": now})

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


# Test edge cases and error handling
async def test_sensor_error_handling(hass, now):
    """Test sensor behavior with invalid or missing data."""
    # Normal sensor
    hass.states.async_set("sensor.co2", "800", {"unit_of_measurement": "ppm", "last_updated": now})

    # Unavailable sensor
    hass.states.async_set("sensor.unavailable", STATE_UNAVAILABLE, {"last_updated": now})

    # Stale sensor
    hass.states.async_set(
        "sensor.stale",
        "100",
        # Stale (>1 hour old)
        {"unit_of_measurement": "ppb", "last_updated": now - timedelta(hours=2)},
    )

    # Non-numeric sensor
    hass.states.async_set("sensor.non_numeric", "not a number", {"last_updated": now})

    # Unknown pollutant type sensor
    hass.states.async_set("sensor.unknown_type", "50", {"unit_of_measurement": "unknown", "last_updated": now})

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

    # Check results - only CO2 at 800 ppm (IAQI 60) should be valid
    native_value = sensor.native_value
    assert native_value is not None
    assert_that(native_value, close_to(60.0, 0.1))

    assert_that(
        sensor.extra_state_attributes["sensor_errors"],
        contains_inanyorder("pm25: no state object", "voc: unavailable", "o3: not numeric", "unknown: bracket unknown"),
    )


async def test_partial_data_log_on_change(hass, memory_handler, now):
    """Test that partial data is logged when the set of sensors with errors changes."""
    # First update - CO2 and PM25 are working, VOC is unavailable
    hass.states.async_set("sensor.co2", "800", {"unit_of_measurement": "ppm", "last_updated": now})

    hass.states.async_set("sensor.pm25", "30", {"unit_of_measurement": "μg/m³", "last_updated": now})

    hass.states.async_set("sensor.voc", STATE_UNAVAILABLE, {"last_updated": now})

    # Create our sensor
    sensor = IndoorAQISensor(
        hass=hass,
        name="Test AQI",
        unique_id="test_aqi",
        sensor_map={"co2": "sensor.co2", "pm25": "sensor.pm25", "voc": "sensor.voc"},
        stale_time=timedelta(hours=1),
    )

    buffer = memory_handler.buffer

    # First update - VOC unavailable
    sensor.update()
    assert_that(buffer, log_containing("partial data"))
    buffer.clear()

    # Second update - same state, no log expected
    sensor.update()
    assert_that(buffer, empty_log())

    # Third update - PM25 becomes unavailable
    hass.states.async_set("sensor.pm25", STATE_UNAVAILABLE, {"last_updated": now})
    sensor.update()
    assert_that(buffer, log_containing("Newly unavailable: pm25"))
    buffer.clear()

    # Fourth update - PM25 back to normal, VOC still unavailable
    hass.states.async_set("sensor.pm25", "30", {"unit_of_measurement": "μg/m³", "last_updated": now})
    sensor.update()
    assert_that(buffer, log_containing("Newly available: pm25"))
    buffer.clear()


async def test_log_after_hour_unchanged(hass, memory_handler, now):
    """Test that partial data is logged again after an hour even if unchanged."""
    # Setup - CO2 working, VOC unavailable
    hass.states.async_set("sensor.co2", "800", {"unit_of_measurement": "ppm", "last_updated": now})

    hass.states.async_set("sensor.voc", STATE_UNAVAILABLE, {"last_updated": now})

    # Create our sensor
    sensor = IndoorAQISensor(
        hass=hass,
        name="Test AQI",
        unique_id="test_aqi",
        sensor_map={"co2": "sensor.co2", "voc": "sensor.voc"},
        stale_time=timedelta(hours=1),
    )

    buffer = memory_handler.buffer

    # First update - VOC unavailable
    sensor.update()
    assert_that(buffer, log_containing("partial data"))
    buffer.clear()

    # Second update - same state, no log expected
    sensor.update()
    assert_that(buffer, empty_log())

    # Instead of patching datetime, just manually adjust the timestamp
    # Set last log time back by an hour to simulate passage of time
    sensor._last_log_time = now - timedelta(hours=1, minutes=1)

    # Update again - should log since it's been over an hour
    sensor.update()
    assert_that(buffer, log_containing("partial data"))


if __name__ == "__main__":
    pytest_bazel.main()
