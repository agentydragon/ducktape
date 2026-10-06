"""The gitstatusd lookup: the configured path first, `PATH` only when none is configured."""

import dataclasses
from pathlib import Path

import pytest
import pytest_bazel

from x.wt.server.gitstatusd_listener import find_gitstatusd


def _gitstatusd(directory: Path, *, exit_code: int = 0) -> Path:
    directory.mkdir()
    binary = directory / "gitstatusd"
    binary.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def on_path(tmp_path, monkeypatch) -> Path:
    """A working `gitstatusd` as the only thing on `PATH`."""
    binary = _gitstatusd(tmp_path / "on_path")
    monkeypatch.setenv("PATH", str(binary.parent))
    return binary


def test_configured_path_wins_over_path(test_config, tmp_path, on_path):
    configured = _gitstatusd(tmp_path / "configured")

    found = find_gitstatusd(dataclasses.replace(test_config, gitstatusd_path=configured))

    assert found == (str(configured), None)


def test_broken_configured_path_does_not_fall_back_to_path(test_config, tmp_path, on_path):
    configured = _gitstatusd(tmp_path / "configured", exit_code=1)

    path, error = find_gitstatusd(dataclasses.replace(test_config, gitstatusd_path=configured))

    assert path is None
    assert error is not None
    assert str(configured) in error


def test_path_is_used_when_nothing_is_configured(test_config, on_path):
    assert find_gitstatusd(dataclasses.replace(test_config, gitstatusd_path=None)) == ("gitstatusd", None)


def test_nothing_configured_and_nothing_on_path_is_an_error(test_config, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    path, error = find_gitstatusd(dataclasses.replace(test_config, gitstatusd_path=None))

    assert path is None
    assert error is not None


if __name__ == "__main__":
    pytest_bazel.main()
