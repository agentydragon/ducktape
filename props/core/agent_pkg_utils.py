"""Agent package utilities for validating packages."""

from __future__ import annotations

import io
import tarfile

# Required files in agent package tar (build context)
DOCKERFILE_FILE = "Dockerfile"


class AgentPkgValidationError(Exception):
    """Raised when agent package validation fails."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        error_list = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"Invalid agent package:\n{error_list}")


def validate_packed_agent_pkg(archive: bytes) -> None:
    """Validate a packed archive has required files for building.

    Only validates Dockerfile presence. /init is validated in the built
    image via agent_pkg.builder.validate_image().

    Raises:
        AgentPkgValidationError: If Dockerfile is missing.
    """
    buffer = io.BytesIO(archive)

    with tarfile.open(fileobj=buffer, mode="r") as tar:
        if DOCKERFILE_FILE not in tar.getnames():
            raise AgentPkgValidationError([f"Missing required file: {DOCKERFILE_FILE}"])
