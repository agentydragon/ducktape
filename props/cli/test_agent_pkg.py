"""Tests for agent-pkg CLI commands."""

from __future__ import annotations

import tarfile
from io import BytesIO

import pytest
import pytest_bazel

from props.core.agent_pkg_utils import DOCKERFILE_FILE, AgentPkgValidationError, validate_packed_agent_pkg


class TestValidatePackedAgentPkg:
    """Tests for validate_packed_agent_pkg function."""

    def test_missing_dockerfile_in_archive(self) -> None:
        """Archive missing Dockerfile raises AgentPkgValidationError."""
        # Create archive directly without Dockerfile
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            # Add init, but no Dockerfile
            init_content = b"#!/bin/bash\necho test"
            info = tarfile.TarInfo(name="init")
            info.size = len(init_content)
            info.mode = 0o755
            tar.addfile(info, BytesIO(init_content))
        archive = buffer.getvalue()

        with pytest.raises(AgentPkgValidationError) as exc_info:
            validate_packed_agent_pkg(archive)
        assert DOCKERFILE_FILE in exc_info.value.errors[0]


if __name__ == "__main__":
    pytest_bazel.main()
