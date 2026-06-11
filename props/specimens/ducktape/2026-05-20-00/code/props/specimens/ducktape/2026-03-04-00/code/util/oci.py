"""Container image utilities: load, push, and manage OCI images in tests.

Combines Docker image loading (via load.sh scripts and tarballs) and OCI push
utilities (via crane) previously spread across test_util.docker, test_util.oci,
and test_util.image_loader.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace

from util.bazel import runfiles

logger = logging.getLogger(__name__)

_CRANE_RLOCATION = "crane/crane"


# ---------------------------------------------------------------------------
# OCI image push via crane
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, available as an OCI layout directory.

    image_rlocation: Runfiles-relative path to the oci_image output directory.
    repo_name: OCI repository name (e.g., "critic", "grader").
    """

    repo_name: str
    image_rlocation: str


async def crane_push(
    image: BazelImage, registry_url: str, tag: str, *, username: str | None = None, password: str | None = None
) -> str:
    """Push an OCI layout directory to a registry via crane.

    Uses asyncio subprocess to avoid blocking the event loop while uvicorn
    serves registry proxy requests on the same loop.

    When username/password are provided, a temporary Docker config is created
    so crane authenticates with the registry proxy.

    Returns the digest (sha256:...) of the pushed image.
    """
    crane = runfiles.get_required_path(_CRANE_RLOCATION)
    image_path = runfiles.get_required_path(image.image_rlocation)
    dest = f"{registry_url}/{image.repo_name}:{tag}"

    env: dict[str, str] | None = None
    with tempfile.TemporaryDirectory(prefix="crane_auth_") as config_dir_str:
        if username and password:
            config_dir = Path(config_dir_str)
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            (config_dir / "config.json").write_text(json.dumps({"auths": {registry_url: {"auth": token}}}))
            env = {**os.environ, "DOCKER_CONFIG": config_dir_str}

        logger.info("Pushing %s -> %s via crane", image_path, dest)
        proc = await asyncio.create_subprocess_exec(
            crane,
            "push",
            str(image_path),
            dest,
            "--insecure",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"crane push failed for {dest}: {stderr.decode()}")
        digest = _parse_crane_digest(stdout.decode().strip(), dest)
        logger.info("Pushed %s: %s", dest, digest)
        return digest


def _parse_crane_digest(stdout: str, dest: str) -> str:
    """Extract digest from crane push output.

    crane push prints the full reference with digest, e.g.:
    'localhost:12345/critic@sha256:abc123...'
    """
    if "@sha256:" in stdout:
        return "sha256:" + stdout.split("@sha256:", 1)[1].split()[0]
    raise RuntimeError(f"crane push did not return digest for {dest}: {stdout!r}")


# ---------------------------------------------------------------------------
# Image loading via Docker/Podman
# ---------------------------------------------------------------------------


tracer = trace.get_tracer(__name__)


def load_image(tarball_rlocation: str) -> None:
    """Load a Docker image tarball into the container runtime.

    Args:
        tarball_rlocation: Runfiles-relative path to the tarball
            (e.g., "_main/third_party/containers/postgres_18_load/tarball.tar").
    """
    tarball_name = tarball_rlocation.rsplit("/", 1)[-1]
    with tracer.start_as_current_span(f"load_image({tarball_name})"):
        with tracer.start_as_current_span("resolve_runfiles_path"):
            tarball_path = runfiles.get_required_path(tarball_rlocation)
        _docker_load(tarball_path, tarball_rlocation)


def _docker_load(tarball_path: Path, tarball_rlocation: str) -> None:
    cmd = shutil.which("docker") or shutil.which("podman")
    if not cmd:
        raise RuntimeError("Neither docker nor podman CLI found")
    result = subprocess.run([cmd, "load", "-i", str(tarball_path)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load image from {tarball_rlocation}: {result.stderr}")


def load_bazel_image(load_script_path: str, image_tag: str) -> str:
    """Load an OCI image from a Bazel oci_load target.

    Args:
        load_script_path: Relative path to the load.sh script (e.g., "third_party/debian_slim/load.sh")
        image_tag: The expected image tag after loading (e.g., "debian-slim:test")

    Returns:
        The image tag that was loaded.

    Raises:
        RuntimeError: If loading the image fails.
    """
    load_script = runfiles.get_required_path(f"_main/{load_script_path}")

    result = subprocess.run(
        [load_script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DOCKER_CLI_EXPERIMENTAL": "enabled"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load image {image_tag}: {result.stderr}")

    return image_tag
