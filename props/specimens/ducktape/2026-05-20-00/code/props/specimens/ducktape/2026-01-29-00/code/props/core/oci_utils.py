"""Utilities for OCI image operations.

Handles:
- Image resolution (tag to digest, pulling images)
- Registry URL configuration
- OCI reference building
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

import requests

from props.core.agent_types import AgentType

if TYPE_CHECKING:
    import aiodocker

logger = logging.getLogger(__name__)

# Registry URL for pulling images (from agent containers on props-agents network)
# The backend container provides the registry proxy at /v2/*
REGISTRY_PROXY_CONTAINER_NAME = os.environ.get("PROPS_PROXY_CONTAINER_NAME", "props-backend")
REGISTRY_PROXY_CONTAINER_PORT = os.environ.get("PROPS_PROXY_CONTAINER_PORT", "8000")

# Registry URL for pulling images (from host)
REGISTRY_HOST = os.environ.get("PROPS_REGISTRY_HOST", "127.0.0.1")
REGISTRY_PORT = os.environ.get("PROPS_REGISTRY_PORT", "8000")

# Proxy URL for registry operations (backend provides registry proxy at /v2/*)
DEFAULT_PROXY_URL = os.environ.get("PROPS_REGISTRY_PROXY_URL", "http://localhost:8000")

# Builtin image tag - used by all Bazel oci_push targets
BUILTIN_TAG = "latest"


# --- Image resolution for containers (async, uses aiodocker) ---


async def resolve_image_ref_async(docker: aiodocker.Docker, image_ref: str) -> str:
    """Resolve an OCI image reference to a Docker image ID.

    Pulls the image if not present locally.

    Args:
        docker: Async Docker client
        image_ref: OCI image reference

    Returns:
        Docker image ID (sha256:...)
    """
    # Normalize the reference (add registry if relative)
    full_ref = normalize_image_ref(image_ref)

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


def normalize_image_ref(image_ref: str) -> str:
    """Normalize image reference, adding registry if needed.

    Args:
        image_ref: Image reference (tag or digest)

    Returns:
        Fully qualified image reference

    Examples:
        "critic:latest" -> "localhost:8000/critic:latest"
        "localhost:8000/critic:latest" -> "localhost:8000/critic:latest"
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


# --- Image resolution via registry proxy (sync, uses requests) ---


def is_digest(ref: str) -> bool:
    """Check if a reference is a digest (sha256:...) vs a tag.

    Args:
        ref: Image reference (tag or digest)

    Returns:
        True if ref is a digest, False if it's a tag
    """
    return bool(re.match(r"^(sha256|sha384|sha512):[a-f0-9]+$", ref))


def resolve_image_ref(agent_type: AgentType, ref: str, *, proxy_url: str | None = None) -> str:
    """Resolve image reference to digest via registry proxy.

    Args:
        agent_type: Agent type (determines repository name via str(agent_type))
        ref: Tag or digest (e.g., "latest", "sha256:abc...")
        proxy_url: Registry proxy URL (defaults to PROPS_REGISTRY_PROXY_URL env var or localhost:8000)

    Returns:
        Digest (sha256:...) - either the provided digest or resolved from tag

    Raises:
        ValueError: If tag doesn't exist or proxy returns error
    """
    # If already a digest, return as-is
    if is_digest(ref):
        logger.debug(f"Reference {ref} is already a digest, returning as-is")
        return ref

    # Repository name is just the string representation of agent type
    repository = str(agent_type)

    # Resolve tag via proxy HEAD request (admin auth)
    proxy = proxy_url or DEFAULT_PROXY_URL
    manifest_url = f"{proxy}/v2/{repository}/manifests/{ref}"
    headers = {"Accept": "application/vnd.oci.image.manifest.v1+json"}

    # Use admin auth from environment (PGUSER/PGPASSWORD)
    pguser = os.environ.get("PGUSER")
    pgpassword = os.environ.get("PGPASSWORD")

    if not pguser or not pgpassword:
        raise ValueError(
            "PGUSER and PGPASSWORD environment variables required for registry authentication. "
            "These should be set in the environment where agent launches occur."
        )

    auth = (pguser, pgpassword)

    logger.info(f"Resolving tag {repository}:{ref} via proxy at {proxy}")

    try:
        resp = requests.head(manifest_url, headers=headers, auth=auth, timeout=10)
    except requests.RequestException as e:
        raise ValueError(f"Failed to resolve tag {repository}:{ref}: {e}")

    if resp.status_code == 404:
        raise ValueError(f"Image not found: {repository}:{ref}")

    if resp.status_code != 200:
        raise ValueError(f"Proxy returned error {resp.status_code} for {repository}:{ref}: {resp.text}")

    digest = resp.headers.get("Docker-Content-Digest")
    if not digest:
        raise ValueError(f"Proxy didn't return Docker-Content-Digest header for {repository}:{ref}")

    logger.info(f"Resolved {repository}:{ref} → {digest}")
    return str(digest)  # Cast to satisfy mypy (already checked non-None above)


def build_oci_reference(agent_type: AgentType, digest: str) -> str:
    """Build full OCI reference from agent type and digest.

    Args:
        agent_type: Agent type (determines repository name via str(agent_type))
        digest: Manifest digest (sha256:...)

    Returns:
        Full OCI reference (host:port/repository@digest)

    Example:
        >>> build_oci_reference(AgentType.CRITIC, "sha256:abc...")
        "localhost:8000/critic@sha256:abc..."
    """
    repository = str(agent_type)
    return f"{REGISTRY_HOST}:{REGISTRY_PORT}/{repository}@{digest}"
