"""OCI image utilities for tests.

Provides async push utilities for Bazel-built OCI images to test registries
using crane. Images are pushed directly from OCI layout directories produced
by Bazel's oci_image rule.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import runfiles

logger = logging.getLogger(__name__)

_CRANE_RLOCATION = "crane/crane"


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, available as an OCI layout directory.

    image_rlocation: Runfiles-relative path to the oci_image output directory.
    repo_name: OCI repository name (e.g., "critic", "grader").
    """

    repo_name: str
    image_rlocation: str


async def crane_push(image: BazelImage, registry_url: str, tag: str) -> None:
    """Push an OCI layout directory to a registry via crane.

    Uses asyncio subprocess to avoid blocking the event loop while uvicorn
    serves registry proxy requests on the same loop.

    Args:
        image: Bazel-built OCI image with layout directory.
        registry_url: Registry host:port (e.g., "localhost:12345").
        tag: Tag to push (e.g., "latest").
    """
    crane = runfiles.get_required_path(_CRANE_RLOCATION)
    image_path = runfiles.get_required_path(image.image_rlocation)
    dest = f"{registry_url}/{image.repo_name}:{tag}"

    logger.info("Pushing %s -> %s via crane", image_path, dest)
    proc = await asyncio.create_subprocess_exec(
        crane,
        "push",
        str(image_path),
        dest,
        "--insecure",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"crane push failed for {dest}: {stderr.decode()}")
    logger.info("Pushed %s: %s", dest, stdout.decode().strip())
