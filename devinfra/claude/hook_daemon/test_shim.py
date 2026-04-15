"""Tests for shim binary resolution."""

from pathlib import Path

import pytest
import pytest_bazel

from devinfra.claude.hook_daemon.shim_install import SHIM_DIR_ENV, resolve_real_binary


def test_resolve_real_binary_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no binary is on PATH, raises FileNotFoundError."""
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv(SHIM_DIR_ENV, raising=False)

    with pytest.raises(FileNotFoundError, match="bazelisk"):
        resolve_real_binary("bazelisk")


def test_resolve_real_binary_skips_shim_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """resolve_real_binary with shim_dir excludes that directory, finding binary elsewhere."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    real_dir = tmp_path / "real"
    real_dir.mkdir()

    # Place "bazelisk" in both dirs; shim_dir version should be skipped.
    for d in (shim_dir, real_dir):
        b = d / "bazelisk"
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)

    monkeypatch.setenv("PATH", f"{shim_dir}:{real_dir}")
    monkeypatch.delenv(SHIM_DIR_ENV, raising=False)

    result = resolve_real_binary("bazelisk", shim_dir=shim_dir)
    assert result == str(real_dir / "bazelisk")


def test_resolve_real_binary_shim_dir_env_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without shim_dir arg, SHIM_DIR_ENV env var is used as fallback (legacy shim wrappers)."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    real_dir = tmp_path / "real"
    real_dir.mkdir()

    for d in (shim_dir, real_dir):
        b = d / "bazelisk"
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)

    monkeypatch.setenv("PATH", f"{shim_dir}:{real_dir}")
    monkeypatch.setenv(SHIM_DIR_ENV, str(shim_dir))

    result = resolve_real_binary("bazelisk")
    assert result == str(real_dir / "bazelisk")


if __name__ == "__main__":
    pytest_bazel.main()
