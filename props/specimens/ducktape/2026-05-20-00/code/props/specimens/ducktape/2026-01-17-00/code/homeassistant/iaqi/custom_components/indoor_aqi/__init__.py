# custom_components/indoor_aqi/__init__.py

import logging

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

DOMAIN = "indoor_aqi"
PLATFORMS = ["sensor"]
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """
    Called once when HA loads our integration via YAML.
    We'll check if there's already an import-based entry; if not, create one.
    """
    if DOMAIN not in config:
        # No YAML config for indoor_aqi
        return True

    # Store the entire YAML block in hass.data so sensor.py can see it
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["yaml_config"] = config[DOMAIN]

    # Check if we've already created an import-based entry
    current_entries = hass.config_entries.async_entries(DOMAIN)
    for entry in current_entries:
        if entry.source == config_entries.SOURCE_IMPORT:
            _LOGGER.debug("IndoorAQI: already have an import entry; not creating another.")
            return True

    # Otherwise, create a new import-based config entry (which triggers config_flow.py)
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=config[DOMAIN],  # pass the entire YAML as data if you want
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Called when the config entry is set up (including the import-based one).
    We'll forward to sensor platform(s).
    """
    _LOGGER.debug("IndoorAQI: async_setup_entry called, setting up sensor.")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry (if user removes it)."""
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return bool(result)
