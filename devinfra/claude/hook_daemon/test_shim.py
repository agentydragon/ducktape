"""Tests for shim binary resolution."""

import os
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.claude.hook_daemon.shim_install import resolve_real_binary


def test_resolve_real_binary_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When no binary is on PATH, raises FileNotFoundError."""
    monkeypatch.setenv("PATH", "/nonexistent")

    with pytest.raises(FileNotFoundError, match="bazelisk"):
        resolve_real_binary("bazelisk", tmp_path)


def test_resolve_real_binary_skips_shim_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """resolve_real_binary excludes shim_dir, finding the binary elsewhere on PATH."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    real_dir = tmp_path / "real"
    real_dir.mkdir()

    # Place "bazelisk" in both dirs; shim_dir version should be skipped.
    for d in (shim_dir, real_dir):
        b = d / "bazelisk"
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)

    monkeypatch.setenv("PATH", os.pathsep.join([str(shim_dir), str(real_dir)]))

    result = resolve_real_binary("bazelisk", shim_dir)
    assert result == str(real_dir / "bazelisk")


if __name__ == "__main__":
    pytest_bazel.main()
