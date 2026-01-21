"""pytest fixtures and global test tweaks."""

from __future__ import annotations

import pathlib

import custom_components
import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, hass):
    """Ensure the custom component & config dir are available for every test.

    The pytest_homeassistant_custom_component plugin includes its own
    custom_components package. We need to add our custom_components path
    to custom_components.__path__ so HA's loader can discover indoor_aqi.
    """
    # Point Home Assistant towards the iaqi directory
    config_dir = pathlib.Path(__file__).resolve().parents[2]
    hass.config.config_dir = str(config_dir)

    # Add our custom_components path to the module's __path__
    # This allows HA's loader to discover our integration
    cc_path = str(config_dir / "custom_components")
    if cc_path not in custom_components.__path__:
        custom_components.__path__.insert(0, cc_path)
