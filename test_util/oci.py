"""OCI image utilities for tests.

Provides crane-based push for Bazel-built OCI images to test registries,
bypassing Docker entirely. Images go directly from Bazel's OCI layout
directory to the registry via crane push.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import runfiles

logger = logging.getLogger(__name__)

_CRANE_RLOCATION = "crane/crane"


def _get_crane() -> Path:
    """Get path to the crane binary from runfiles."""
    return runfiles.get_required_path(_CRANE_RLOCATION)


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, identified by its layout directory.

    The layout directory is the output of a Bazel oci_image target,
    containing the standard OCI image layout (blobs/, index.json, oci-layout).

    repo_name matches the OCI repository name (and AgentType for agent images).
    """

    repo_name: str
    layout_dir: Path


def crane_push(image: BazelImage, registry_url: str, tag: str) -> None:
    """Push an OCI image layout to a registry via crane.

    Args:
        image: Bazel-built OCI image with layout directory.
        registry_url: Registry host:port (e.g., "localhost:12345").
        tag: Tag to push (e.g., "latest").
    """
    crane = _get_crane()
    dest = f"{registry_url}/{image.repo_name}:{tag}"
    logger.info("Pushing %s -> %s", image.layout_dir, dest)
    result = subprocess.run(
        [str(crane), "push", "--insecure", str(image.layout_dir), dest], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"crane push failed for {dest}: {result.stderr}")
    logger.info("Pushed %s", dest)
