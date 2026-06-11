"""pytest fixtures and global test tweaks."""

from __future__ import annotations

import pathlib

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, hass):
    """Ensure the custom component & config dir are available for every test."""
    # Point Home Assistant towards the repository root so that the loader can
    # discover the *custom_components* folder (and therefore our integration)
    hass.config.config_dir = str(pathlib.Path(__file__).resolve().parents[1])
