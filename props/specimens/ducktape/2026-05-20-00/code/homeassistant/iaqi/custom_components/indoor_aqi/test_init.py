"""Test Indoor AQI setup."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest_bazel
from hamcrest import assert_that, has_entries

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

DOMAIN = "indoor_aqi"


async def test_setup_component(hass: HomeAssistant):
    """Test setting up the Indoor AQI component."""
    # Import inside test function to avoid loading HA modules before
    # pytest_homeassistant_custom_component can patch them
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    # Define a basic YAML config
    config = {
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

    # Mock entity states - we don't need actual states for this test
    hass.states.async_set("sensor.test_co2", "800")
    hass.states.async_set("sensor.test_pm25", "30")

    # Set up the component
    assert await async_setup_component(hass, DOMAIN, config)
    await hass.async_block_till_done()

    # Verify that the component initialized correctly
    assert_that(hass.data[DOMAIN], has_entries(yaml_config=config[DOMAIN]))


async def test_setup_entry(hass: HomeAssistant):
    """Test setting up a config entry."""
    from custom_components.indoor_aqi import async_setup_entry  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: PLC0415

    # Create a mock entry
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="test_entry_id")

    # We expect async_setup_entry to call async_forward_entry_setups for sensor platform
    with patch("homeassistant.config_entries.ConfigEntries.async_forward_entry_setups") as mock_forward:
        assert await async_setup_entry(hass, entry)
        # Check that it forwarded the setup to the sensor platform
        mock_forward.assert_called_once_with(entry, ["sensor"])


async def test_unload_entry(hass: HomeAssistant):
    """Test unloading a config entry."""
    from custom_components.indoor_aqi import async_unload_entry  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: PLC0415

    # Create a mock entry
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="test_entry_id")

    # We expect async_unload_entry to call async_unload_platforms
    with patch("homeassistant.config_entries.ConfigEntries.async_unload_platforms") as mock_unload:
        # Need to add hass.data for the entry first
        hass.data.setdefault(DOMAIN, {})

        assert await async_unload_entry(hass, entry)

        # Check that it unloaded the platforms
        mock_unload.assert_called_once_with(entry, ["sensor"])


if __name__ == "__main__":
    pytest_bazel.main()
