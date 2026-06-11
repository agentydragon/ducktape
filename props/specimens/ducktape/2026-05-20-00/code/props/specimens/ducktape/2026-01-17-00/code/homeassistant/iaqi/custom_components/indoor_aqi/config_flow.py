# custom_components/indoor_aqi/config_flow.py

import logging

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


class IndoorAQIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Indoor AQI."""

    VERSION = 1

    async def async_step_import(self, user_input=None) -> ConfigFlowResult:
        """Handle YAML import."""
        # user_input might contain data we pass in from __init__.py,
        # e.g. the entire monitors list. Let's store it in the entry's data.
        if user_input is None:
            user_input = {}

        _LOGGER.debug("IndoorAQI: YAML import config flow triggered with: %s", user_input)

        # We only want a single import-based entry. If one already exists, skip.
        await self.async_set_unique_id("indoor_aqi_yaml_import")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title="Indoor AQI (imported via YAML)", data=user_input)
