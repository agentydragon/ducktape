"""pytest fixtures and global test tweaks."""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, hass):
    """Ensure the custom component & config dir are available for every test."""
    # Point Home Assistant towards the iaqi directory so that HA can discover
    # the custom_components folder (and therefore our integration)
    hass.config.config_dir = str(pathlib.Path(__file__).resolve().parents[2])
