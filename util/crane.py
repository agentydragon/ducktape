"""Crane (go-containerregistry) CLI wrapper.

Resolves the crane binary from Bazel runfiles (@crane) and provides sync/async
push helpers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from util.bazel import runfiles
from util.oci import write_docker_auth

logger = logging.getLogger(__name__)

_CRANE_RLOCATION = "crane/crane"


def get_crane() -> Path:
    """Resolve the crane binary from Bazel runfiles."""
    return runfiles.get_required_path(_CRANE_RLOCATION)


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, available as an OCI layout directory."""

    repo_name: str
    image_rlocation: str


async def crane_push(
    image: BazelImage, registry_url: str, tag: str, *, username: str | None = None, password: str | None = None
) -> str:
    """Push an OCI layout directory to a registry via crane.

    Uses asyncio subprocess to avoid blocking the event loop.
    Returns the digest (sha256:...) of the pushed image.
    """
    crane = get_crane()
    image_path = runfiles.get_required_path(image.image_rlocation)
    dest = f"{registry_url}/{image.repo_name}:{tag}"

    env: dict[str, str] | None = None
    with tempfile.TemporaryDirectory(prefix="crane_auth_") as config_dir_str:
        if username and password:
            write_docker_auth(registry_url, username, password, Path(config_dir_str))
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
    if "@sha256:" in stdout:
        return "sha256:" + stdout.split("@sha256:", 1)[1].split()[0]
    raise RuntimeError(f"crane push did not return digest for {dest}: {stdout!r}")
