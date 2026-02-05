"""OCI image utilities for tests.

Provides push utilities for Bazel-built OCI images to test registries.
Images are loaded via `docker load` from oci_load tarballs and pushed
via `docker push` in Docker v2 manifest format (compatible with all
Docker daemons).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

import runfiles

logger = logging.getLogger(__name__)


def _docker_cmd() -> str:
    """Find docker or podman CLI."""
    cmd = shutil.which("docker") or shutil.which("podman")
    if not cmd:
        raise RuntimeError("Neither docker nor podman CLI found")
    return cmd


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, available as a docker-load tarball.

    tarball_rlocation: Runfiles-relative path to the oci_load tarball.
    repo_name: OCI repository name (e.g., "critic", "grader").
    load_tag: Tag assigned by oci_load (e.g., "critic:latest").
    """

    repo_name: str
    tarball_rlocation: str
    load_tag: str


def docker_push(image: BazelImage, registry_url: str, tag: str) -> None:
    """Load an image from tarball and push to a registry via docker.

    Produces Docker v2 manifest format, which is compatible with all
    Docker daemons (unlike OCI manifests from crane push).

    Args:
        image: Bazel-built OCI image with tarball.
        registry_url: Registry host:port (e.g., "localhost:12345").
        tag: Tag to push (e.g., "latest").
    """
    cmd = _docker_cmd()
    tarball_path = runfiles.get_required_path(image.tarball_rlocation)
    dest = f"{registry_url}/{image.repo_name}:{tag}"

    # Load image from oci_load tarball (produces Docker-format image locally)
    logger.info("Loading image from %s via %s", tarball_path, cmd)
    result = subprocess.run([cmd, "load", "-i", str(tarball_path)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker load failed for {image.tarball_rlocation}: {result.stderr}")
    logger.info("Loaded image: %s", result.stdout.strip())

    # Tag for registry destination
    logger.info("Tagging %s -> %s", image.load_tag, dest)
    result = subprocess.run([cmd, "tag", image.load_tag, dest], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker tag failed for {dest}: {result.stderr}")

    # Push to registry (Docker v2 manifest format).
    # Docker exempts localhost from TLS requirements, so no --insecure-registry needed.
    # For podman, --tls-verify=false is needed but we detect the CLI to avoid flag errors.
    logger.info("Pushing %s", dest)
    push_cmd = [cmd, "push"]
    if "podman" in cmd:
        push_cmd.append("--tls-verify=false")
    push_cmd.append(dest)
    result = subprocess.run(push_cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker push failed for {dest}: {result.stderr}")
    logger.info("Pushed %s", dest)
