from __future__ import annotations

import os

# Default image used across runtime exec and policy evaluation containers.
DEFAULT_RUNTIME_IMAGE = "adgn-runtime:latest"


def resolve_runtime_image(*, fallback: str = DEFAULT_RUNTIME_IMAGE) -> str:
    """Return the Docker image tag used for runtime + policy evaluation flows."""
    img = os.getenv("ADGN_RUNTIME_IMAGE")
    if img:
        return img
    return fallback
