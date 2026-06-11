"""Build Docker images from agent definitions.

Supports two sources:
- Directories (Path): For editor agents, where definitions live in the repo filesystem
- Archives (bytes): For props agents, where definitions are stored in DB as tar archives
  (implemented by unpacking to temp dir and building from directory)

Images must contain /init (executable) which outputs the system prompt when run.

Uses `docker buildx build` for BuildKit cache mount support.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tarfile
import tempfile
from pathlib import Path

import aiodocker

logger = logging.getLogger(__name__)

IMAGE_TAG_PREFIX = "adgn-def"
HASH_LENGTH = 12

# Image contract: /init must exist and be executable
IMAGE_INIT_PATH = "/init"


def _compute_hash(data: bytes) -> str:
    """SHA256 hash, first HASH_LENGTH chars."""
    return hashlib.sha256(data).hexdigest()[:HASH_LENGTH]


def _make_tag(hash_str: str) -> str:
    return f"{IMAGE_TAG_PREFIX}:{hash_str}"


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

    Args:
        docker: aiodocker client
        context_dir: Build context directory
        tag: Image tag
        dockerfile: Path to Dockerfile relative to context_dir (default: "Dockerfile")

    Returns:
        Image ID
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


async def ensure_image_from_archive(docker: aiodocker.Docker, archive: bytes) -> str:
    """Unpack archive to temp dir, ensure_image with archive hash as tag."""
    tag = _make_tag(_compute_hash(archive))

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
            tar.extractall(tmppath, filter="data")
        return await ensure_image(docker, tmppath, tag)


class ImageValidationError(Exception):
    """Raised when an image doesn't meet the required contract."""


async def validate_image(docker: aiodocker.Docker, image_id: str) -> None:
    """Verify IMAGE_INIT_PATH exists in image.

    Creates a temporary container and checks via get_archive.
    """
    container = await docker.containers.create(
        config={
            "Image": image_id,
            "Cmd": ["true"],  # Won't actually run
        }
    )

    try:
        try:
            await container.get_archive(IMAGE_INIT_PATH)
        except aiodocker.DockerError as e:
            if e.status == 404:
                raise ImageValidationError(f"Missing {IMAGE_INIT_PATH} in image")
            raise

    finally:
        await container.delete()
