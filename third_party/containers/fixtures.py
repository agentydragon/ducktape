"""Pytest fixture for the debian-slim base image."""

import pytest

from third_party.containers.rlocations import DEBIAN_SLIM
from util.oci import load_oci_image


@pytest.fixture(scope="session")
def debian_slim_image():
    """Load debian-slim image into Docker daemon and return its tag."""
    return load_oci_image(DEBIAN_SLIM)
