"""Test Indoor AQI setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_bazel
from hamcrest import assert_that, close_to, equal_to, has_entries

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.setup import async_setup_component

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

DOMAIN = "indoor_aqi"


@pytest.fixture
def yaml_config(hass: HomeAssistant) -> dict:
    """YAML config for one monitor, with its source sensors present in the state machine."""
    hass.states.async_set("sensor.test_co2", "800")
    hass.states.async_set("sensor.test_pm25", "30")
    return {
        DOMAIN: {
            "monitors": [
                {
                    "name": "Test AQI",
                    "unique_id": "test_aqi",
                    "sensors": {"co2": "sensor.test_co2", "pm25": "sensor.test_pm25"},
                }
            ],
            "stale_time": "3600",
        }
    }


async def test_setup_component(hass: HomeAssistant, yaml_config: dict):
    """Test setting up the Indoor AQI component."""
    assert await async_setup_component(hass, DOMAIN, yaml_config)
    await hass.async_block_till_done()

    # Verify that the component initialized correctly
    assert_that(hass.data[DOMAIN], has_entries(yaml_config=yaml_config[DOMAIN]))


async def test_entry_loads_and_unloads_sensor(hass: HomeAssistant, yaml_config: dict):
    """The YAML-imported entry loads the sensor platform; unloading takes the sensor offline."""
    assert await async_setup_component(hass, DOMAIN, yaml_config)
    await hass.async_block_till_done()

    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert_that(entry.state, equal_to(ConfigEntryState.LOADED))
    sensor_state = hass.states.get("sensor.test_aqi")
    assert sensor_state is not None
    # CO2 800 ppm is the bottleneck: halfway between the 600 ppm (80) and 1000 ppm (60) breakpoints.
    assert_that(float(sensor_state.state), close_to(70.0, 0.1))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert_that(entry.state, equal_to(ConfigEntryState.NOT_LOADED))
    sensor_state = hass.states.get("sensor.test_aqi")
    assert sensor_state is not None
    assert sensor_state.state == STATE_UNAVAILABLE


if __name__ == "__main__":
    pytest_bazel.main()
