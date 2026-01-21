"""Test the Indoor AQI config flow.

NOTE: These tests are skipped because pytest_homeassistant_custom_component
doesn't work correctly with Bazel's test environment. The plugin requires
HA modules not be imported before it patches them, and HA's integration
loader can't find custom components in Bazel's runfiles structure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_bazel
from hamcrest import assert_that, contains_string, equal_to, has_entries

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.skip(reason="pytest_homeassistant_custom_component incompatible with Bazel")

DOMAIN = "indoor_aqi"


async def _init_config_flow(hass: HomeAssistant, source: str | None = None, data: dict | None = None):
    """Initialize a configuration flow with the given source and data."""
    # Lazy imports to avoid loading homeassistant modules before pytest plugin patches them
    from homeassistant import config_entries as ce  # noqa: PLC0415

    if source is None:
        source = ce.SOURCE_IMPORT
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": source}, data=data)


async def test_import_flow(hass: HomeAssistant):
    """Test the import flow."""
    # Define some test data
    test_data = {"monitors": [{"name": "Test AQI", "sensors": {"co2": "sensor.test_co2", "pm25": "sensor.test_pm25"}}]}

    # Start the import flow
    result = await _init_config_flow(hass, data=test_data)

    # Check that it created the entry
    assert_that(result, has_entries(type="create_entry", data=test_data))
    assert_that(result["title"], contains_string("imported via YAML"))

    # Check that the entry got the right unique ID
    assert_that(result["result"].unique_id, equal_to("indoor_aqi_yaml_import"))


async def test_import_flow_already_exists(hass: HomeAssistant):
    """Test the import flow when an entry already exists."""
    # Lazy imports to avoid loading homeassistant modules before pytest plugin patches them
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: PLC0415

    # Create an existing entry with the same unique ID
    MockConfigEntry(domain=DOMAIN, unique_id="indoor_aqi_yaml_import", data={}).add_to_hass(hass)

    # Try to import again
    test_data = {"monitors": [{"name": "Test AQI", "sensors": {}}]}
    result = await _init_config_flow(hass, data=test_data)

    # Check that it aborted
    assert_that(result, has_entries(type="abort", reason="already_configured"))


async def test_import_flow_empty_data(hass: HomeAssistant):
    """Test the import flow with empty data."""
    # Try to import with empty data
    result = await _init_config_flow(hass, data=None)

    # Check that it still works (creates an entry with empty data)
    assert_that(result, has_entries(type="create_entry", data={}))


if __name__ == "__main__":
    pytest_bazel.main()
