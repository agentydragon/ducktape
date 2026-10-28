"""Test Indoor AQI setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_bazel
from custom_components.indoor_aqi import async_setup
from hamcrest import assert_that, close_to, equal_to

from homeassistant import config_entries
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


async def _load_then_unload_import_entry(hass: HomeAssistant, config: dict):
    """Create a real YAML-imported entry and leave it ready for config refresh."""
    assert await async_setup_component(hass, DOMAIN, config)
    await hass.async_block_till_done()

    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert_that(entry.source, equal_to(config_entries.SOURCE_IMPORT))
    assert_that(entry.state, equal_to(ConfigEntryState.LOADED))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert_that(entry.state, equal_to(ConfigEntryState.NOT_LOADED))
    return entry


async def test_setup_component(hass: HomeAssistant, yaml_config: dict):
    """Test setting up the Indoor AQI component."""
    assert await async_setup_component(hass, DOMAIN, yaml_config)
    await hass.async_block_till_done()

    # The imported config entry is the single source of truth for YAML config.
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert_that(entry.data, equal_to(yaml_config[DOMAIN]))


async def test_existing_import_entry_uses_latest_yaml_on_startup(hass: HomeAssistant, yaml_config: dict):
    """An existing import entry is refreshed before its sensors load during startup."""
    old_config = {
        DOMAIN: {"monitors": [{"name": "Old AQI", "unique_id": "old_aqi", "sensors": {"co2": "sensor.old_co2"}}]}
    }
    hass.states.async_set("sensor.old_co2", "800")
    entry = await _load_then_unload_import_entry(hass, old_config)

    assert await async_setup(hass, yaml_config)
    assert_that(entry.data, equal_to(yaml_config[DOMAIN]))
    assert_that(entry.state, equal_to(ConfigEntryState.NOT_LOADED))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert_that(len(entries), equal_to(1))
    (entry,) = entries
    assert_that(entry.data, equal_to(yaml_config[DOMAIN]))
    assert_that(entry.state, equal_to(ConfigEntryState.LOADED))
    sensor_state = hass.states.get("sensor.test_aqi")
    assert sensor_state is not None
    assert_that(float(sensor_state.state), close_to(70.0, 0.1))


async def test_existing_import_entry_is_cleared_when_yaml_is_missing(hass: HomeAssistant):
    """An imported entry does not recreate sensors after the YAML block is removed."""
    old_config = {
        DOMAIN: {"monitors": [{"name": "Old AQI", "unique_id": "old_aqi", "sensors": {"co2": "sensor.old_co2"}}]}
    }
    hass.states.async_set("sensor.old_co2", "800")
    entry = await _load_then_unload_import_entry(hass, old_config)

    assert await async_setup(hass, {})
    assert_that(entry.data, equal_to({}))
    assert_that(entry.state, equal_to(ConfigEntryState.NOT_LOADED))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert_that(len(entries), equal_to(1))
    (entry,) = entries
    assert_that(entry.data, equal_to({}))
    assert_that(entry.state, equal_to(ConfigEntryState.LOADED))
    sensor_state = hass.states.get("sensor.old_aqi")
    assert sensor_state is None or sensor_state.state == STATE_UNAVAILABLE


async def test_loaded_import_entry_reloads_when_yaml_changes(hass: HomeAssistant, yaml_config: dict):
    """A runtime YAML update refreshes the entry and reloads its sensor platform."""
    assert await async_setup_component(hass, DOMAIN, yaml_config)
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert_that(entry.state, equal_to(ConfigEntryState.LOADED))

    hass.states.async_set("sensor.updated_co2", "400")
    updated_config = {
        DOMAIN: {
            "monitors": [{"name": "Test AQI", "unique_id": "test_aqi", "sensors": {"co2": "sensor.updated_co2"}}],
            "stale_time": "3600",
        }
    }

    assert await async_setup(hass, updated_config)
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert_that(len(entries), equal_to(1))
    assert_that(entries[0].data, equal_to(updated_config[DOMAIN]))
    assert_that(entries[0].state, equal_to(ConfigEntryState.LOADED))
    sensor_state = hass.states.get("sensor.test_aqi")
    assert sensor_state is not None
    assert_that(float(sensor_state.state), close_to(100.0, 0.1))


async def test_loaded_import_entry_clears_when_yaml_is_removed(hass: HomeAssistant, yaml_config: dict):
    """Removing the YAML block reloads an imported entry without its old sensors."""
    assert await async_setup_component(hass, DOMAIN, yaml_config)
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert_that(entry.state, equal_to(ConfigEntryState.LOADED))

    assert await async_setup(hass, {})
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert_that(len(entries), equal_to(1))
    assert_that(entries[0].data, equal_to({}))
    assert_that(entries[0].state, equal_to(ConfigEntryState.LOADED))
    sensor_state = hass.states.get("sensor.test_aqi")
    assert sensor_state is None or sensor_state.state == STATE_UNAVAILABLE


async def test_missing_yaml_does_not_create_import_entry(hass: HomeAssistant):
    """Setup without the YAML block does not create an import flow."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []


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
