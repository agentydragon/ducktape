"""Pytest fixture for the debian-slim base image."""

import pytest

from devinfra.oci.oci import load_oci_image
from third_party.containers import debian_slim


@pytest.fixture(scope="session")
def debian_slim_image():
    """Load debian-slim image into Docker daemon and return its tag."""
    return load_oci_image(debian_slim.LAYOUT_RLOCATION)
