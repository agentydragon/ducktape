"""Pre-load OCI image tarballs into the local container runtime.

Used by test fixtures to pre-load Bazel-bundled container images (from
oci_tarball) so Testcontainers doesn't need to pull from Docker Hub.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

import runfiles

logger = logging.getLogger(__name__)


def load_image(tarball_rlocation: str) -> None:
    """Load a Docker image tarball into the container runtime.

    Args:
        tarball_rlocation: Runfiles-relative path to the tarball
            (e.g., "_main/props/testing/fixtures/postgres_16_tarball/tarball.tar").
    """
    tarball_path = runfiles.get_required_path(tarball_rlocation)
    cmd = shutil.which("docker") or shutil.which("podman")
    if not cmd:
        raise RuntimeError("Neither docker nor podman CLI found")
    logger.info("Loading image from %s via %s", tarball_path, cmd)
    result = subprocess.run([cmd, "load", "-i", str(tarball_path)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load image from {tarball_rlocation}: {result.stderr}")
    logger.info("Loaded image: %s", result.stdout.strip())
