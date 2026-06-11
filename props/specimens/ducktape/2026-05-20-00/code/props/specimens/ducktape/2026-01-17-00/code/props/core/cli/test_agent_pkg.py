"""Tests for agent-pkg CLI commands."""

from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from props.core.agent_pkg_utils import DOCKERFILE_FILE, AgentPkgValidationError, validate_packed_agent_pkg
from props.core.cli.cmd_agent_pkg import app


@pytest.fixture
def runner() -> CliRunner:
    """CLI test runner."""
    return CliRunner()


def _create_minimal_dockerfile(path: Path) -> None:
    """Create a minimal Dockerfile that copies init."""
    (path / DOCKERFILE_FILE).write_text("FROM python:3.12-slim\nCOPY init /init\nRUN chmod +x /init\n")


@pytest.fixture
def valid_pkg(tmp_path: Path) -> Path:
    """Create a valid agent package directory with Dockerfile and init."""
    _create_minimal_dockerfile(tmp_path)

    init_script = tmp_path / "init"
    init_script.write_text("#!/bin/bash\necho 'initialized'\n")
    init_script.chmod(0o755)

    return tmp_path


@pytest.fixture
def pkg_missing_dockerfile(tmp_path: Path) -> Path:
    """Create package with missing Dockerfile."""
    init_script = tmp_path / "init"
    init_script.write_text("#!/bin/bash\necho 'initialized'\n")
    init_script.chmod(0o755)
    return tmp_path


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


class TestCmdValidate:
    """Tests for validate CLI command."""

    def test_valid_pkg_success(self, runner: CliRunner, valid_pkg: Path) -> None:
        """Valid package exits with code 0."""
        result = runner.invoke(app, ["validate", str(valid_pkg)])
        assert result.exit_code == 0
        assert "Valid agent package" in result.output

    def test_invalid_pkg_fails(self, runner: CliRunner, pkg_missing_dockerfile: Path) -> None:
        """Invalid package exits with non-zero code (exception propagates)."""
        result = runner.invoke(app, ["validate", str(pkg_missing_dockerfile)])
        assert result.exit_code != 0
