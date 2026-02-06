"""OCI image utilities for tests.

Provides async push utilities for Bazel-built OCI images to test registries
using crane. Images are pushed directly from OCI layout directories produced
by Bazel's oci_image rule.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

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


async def crane_push(
    image: BazelImage, registry_url: str, tag: str, *, username: str | None = None, password: str | None = None
) -> None:
    """Push an OCI layout directory to a registry via crane.

    Uses asyncio subprocess to avoid blocking the event loop while uvicorn
    serves registry proxy requests on the same loop.

    When username/password are provided, a temporary Docker config is created
    so crane authenticates with the registry proxy.
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
        logger.info("Pushed %s: %s", dest, stdout.decode().strip())
