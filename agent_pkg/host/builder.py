"""Build Docker images from agent definitions.

Builds images from directory-based agent definitions using `docker buildx build`
for BuildKit cache mount support.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiodocker

logger = logging.getLogger(__name__)

IMAGE_TAG_PREFIX = "adgn-def"


async def _run_docker_buildx(context_dir: Path, tag: str, dockerfile: str = "Dockerfile") -> None:
    """Run docker buildx build with cache mounts enabled."""
    cmd = [
        "docker",
        "buildx",
        "build",
        "--tag",
        tag,
        "--file",
        str(context_dir / dockerfile),
        "--load",  # Load into local docker images
        str(context_dir),
    ]

    logger.info("Running: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"Docker buildx build failed (exit {proc.returncode})")


async def ensure_image(docker: aiodocker.Docker, context_dir: Path, tag: str, *, dockerfile: str = "Dockerfile") -> str:
    """Build image from directory if tag doesn't exist, return image ID.

    The directory must contain a Dockerfile (or the file specified by dockerfile).
    Uses docker buildx for BuildKit cache mount support.
    """
    # Check cache
    try:
        image_info = await docker.images.inspect(tag)
        logger.debug("Image cache hit: %s", tag)
        return str(image_info["Id"])
    except aiodocker.DockerError as e:
        if e.status != 404:
            raise

    # Build using docker buildx
    logger.info("Building image: %s -> %s", context_dir, tag)
    await _run_docker_buildx(context_dir, tag, dockerfile=dockerfile)

    image_info = await docker.images.inspect(tag)
    image_id = str(image_info["Id"])
    logger.info("Built image: %s (%s)", tag, image_id[:19])
    return image_id
