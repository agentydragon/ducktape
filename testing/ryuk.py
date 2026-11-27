"""Preload Testcontainers' pinned cleanup sidecar."""

from testcontainers.core import testcontainers_config

from devinfra.oci.oci import load_oci_image
from third_party.containers import ryuk


def load_ryuk_image() -> None:
    """Install the pinned layout under the tag Testcontainers will look up."""
    load_oci_image(ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
