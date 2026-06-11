"""Utilities for OCI image operations."""

from __future__ import annotations

import logging
import os
import re

import requests

from props.core.agent_types import AgentType
from props.core.registry.images import REGISTRY_HOST, REGISTRY_PORT

logger = logging.getLogger(__name__)

# Proxy URL for registry operations
DEFAULT_PROXY_URL = os.environ.get("PROPS_REGISTRY_PROXY_URL", "http://localhost:5050")

# Builtin image tag - used by all Bazel oci_push targets
BUILTIN_TAG = "latest"


def is_digest(ref: str) -> bool:
    """Check if a reference is a digest (sha256:...) vs a tag.

    Args:
        ref: Image reference (tag or digest)

    Returns:
        True if ref is a digest, False if it's a tag
    """
    return bool(re.match(r"^(sha256|sha384|sha512):[a-f0-9]+$", ref))


def resolve_image_ref(agent_type: AgentType, ref: str, *, proxy_url: str | None = None) -> str:
    """Resolve image reference to digest.

    Args:
        agent_type: Agent type (determines repository name via str(agent_type))
        ref: Tag or digest (e.g., "latest", "sha256:abc...")
        proxy_url: Registry proxy URL (defaults to PROPS_REGISTRY_PROXY_URL env var or localhost:5050)

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
        "localhost:5050/critic@sha256:abc..."
    """
    repository = str(agent_type)
    return f"{REGISTRY_HOST}:{REGISTRY_PORT}/{repository}@{digest}"
