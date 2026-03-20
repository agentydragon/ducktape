"""Tests for bazel_server_warmup module."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest_bazel

from devinfra.claude.bazel_server_warmup import warmup_bazel_server


async def test_warmup_success(tmp_path: Path):
    env_file = tmp_path / "env"
    env_file.write_text("# empty\n")

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"server_pid: 12345\noutput_base: /tmp/bazel\n", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await warmup_bazel_server(
            wrapper_path=Path("/fake/bin/bazel"), project_dir=Path("/fake/project"), env_file=env_file
        )


async def test_warmup_failure(tmp_path: Path):
    env_file = tmp_path / "env"
    env_file.write_text("# empty\n")

    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"ERROR: something\n"))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await warmup_bazel_server(
            wrapper_path=Path("/fake/bin/bazel"), project_dir=Path("/fake/project"), env_file=env_file
        )


async def test_warmup_timeout(tmp_path: Path):
    env_file = tmp_path / "env"
    env_file.write_text("# empty\n")

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(side_effect=TimeoutError)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await warmup_bazel_server(
            wrapper_path=Path("/fake/bin/bazel"), project_dir=Path("/fake/project"), env_file=env_file
        )


if __name__ == "__main__":
    pytest_bazel.main()
