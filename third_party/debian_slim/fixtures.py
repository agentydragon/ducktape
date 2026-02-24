"""Pytest fixture for the debian-slim base image (//third_party/debian_slim:load)."""

import pytest

from third_party.debian_slim.rlocations import IMAGE_TAG, LOAD_SCRIPT
from util.oci import load_bazel_image


@pytest.fixture(scope="session")
def debian_slim_image():
    """Load debian-slim image from Bazel //third_party/debian_slim:load target."""
    return load_bazel_image(LOAD_SCRIPT, IMAGE_TAG)
