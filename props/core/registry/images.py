"""Image resolution for agent containers.

Handles resolving image references to Docker image IDs, supporting both:
- OCI image refs (e.g., "localhost:5050/critic:latest" or "sha256:...")
- Legacy definition archives (tarball + Dockerfile in DB)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiodocker

logger = logging.getLogger(__name__)

# Registry URL for pulling images (from agent containers on props-agents network)
REGISTRY_PROXY_CONTAINER_NAME = os.environ.get("PROPS_REGISTRY_PROXY_CONTAINER_NAME", "props-registry-proxy")
REGISTRY_PROXY_CONTAINER_PORT = os.environ.get("PROPS_REGISTRY_PROXY_CONTAINER_PORT", "5051")

# Registry URL for pulling images (from host)
REGISTRY_HOST = os.environ.get("PROPS_REGISTRY_HOST", "127.0.0.1")
REGISTRY_PORT = os.environ.get("PROPS_REGISTRY_PORT", "5050")


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
