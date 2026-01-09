"""Image resolution for agent containers.

Handles resolving image references to Docker image IDs, supporting both:
- OCI image refs (e.g., "localhost:5050/critic:latest" or "sha256:...")
- Legacy definition archives (tarball + Dockerfile in DB)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from agent_pkg.host.builder import ensure_image_from_archive
from props.core.agent_handle import load_definition_archive
from props.core.ids import DefinitionId

if TYPE_CHECKING:
    import aiodocker

logger = logging.getLogger(__name__)

# Registry URL for pulling images (from agent containers on props-agents network)
REGISTRY_PROXY_CONTAINER_NAME = os.environ.get("PROPS_REGISTRY_PROXY_CONTAINER_NAME", "props-registry-proxy")
REGISTRY_PROXY_CONTAINER_PORT = os.environ.get("PROPS_REGISTRY_PROXY_CONTAINER_PORT", "5051")

# Registry URL for pulling images (from host)
REGISTRY_HOST = os.environ.get("PROPS_REGISTRY_HOST", "127.0.0.1")
REGISTRY_PORT = os.environ.get("PROPS_REGISTRY_PORT", "5050")


async def resolve_image_id(
    docker: aiodocker.Docker, *, image_ref: str | None = None, definition_id: DefinitionId | None = None
) -> str:
    """Resolve an image reference or definition ID to a Docker image ID.

    Priority:
    1. If image_ref is provided, pull/inspect that image
    2. If definition_id is provided, build from tarball archive

    Args:
        docker: Async Docker client
        image_ref: OCI image reference (e.g., "localhost:5050/critic:latest")
        definition_id: Legacy definition ID to load archive from

    Returns:
        Docker image ID (sha256:...)

    Raises:
        ValueError: If neither image_ref nor definition_id is provided, or if resolution fails
    """
    if image_ref is None and definition_id is None:
        raise ValueError("Either image_ref or definition_id must be provided")

    if image_ref is not None:
        return await _resolve_image_ref(docker, image_ref)

    # Legacy path: build from definition archive
    assert definition_id is not None
    archive = load_definition_archive(definition_id)
    image_id = await ensure_image_from_archive(docker, archive)
    logger.info(f"Built image {image_id[:19]} from definition {definition_id}")
    return image_id


async def _resolve_image_ref(docker: aiodocker.Docker, image_ref: str) -> str:
    """Resolve an OCI image reference to a Docker image ID.

    Pulls the image if not present locally.

    Args:
        docker: Async Docker client
        image_ref: OCI image reference

    Returns:
        Docker image ID (sha256:...)
    """
    # Normalize the reference (add registry if relative)
    full_ref = _normalize_image_ref(image_ref)

    # Check if image exists locally
    try:
        image = await docker.images.inspect(full_ref)
        image_id: str = image["Id"]
        logger.info(f"Using cached image {image_id[:19]} for {full_ref}")
        return image_id
    except Exception:
        pass  # Image not found locally, need to pull

    # Pull the image
    logger.info(f"Pulling image {full_ref}")
    try:
        await docker.pull(full_ref)
        image = await docker.images.inspect(full_ref)
        image_id = image["Id"]
        logger.info(f"Pulled image {image_id[:19]} for {full_ref}")
        return image_id
    except Exception as e:
        raise ValueError(f"Failed to pull image {full_ref}: {e}") from e


def _normalize_image_ref(image_ref: str) -> str:
    """Normalize image reference, adding registry if needed.

    Args:
        image_ref: Image reference (tag or digest)

    Returns:
        Fully qualified image reference

    Examples:
        "critic:latest" -> "localhost:5050/critic:latest"
        "localhost:5050/critic:latest" -> "localhost:5050/critic:latest"
        "sha256:abc..." -> "sha256:abc..." (digest refs are not normalized)
    """
    # Digest refs don't need normalization
    if image_ref.startswith("sha256:"):
        return image_ref

    # Already fully qualified
    if "/" in image_ref and ":" in image_ref.split("/")[0]:
        return image_ref

    # Add default registry
    return f"{REGISTRY_HOST}:{REGISTRY_PORT}/{image_ref}"
