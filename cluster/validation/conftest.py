"""Shared fixtures for cluster validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from python.runfiles import Runfiles


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The materialized runfiles checkout, anchored by its root MODULE.bazel."""
    return Path(str(Runfiles.CreateOrRaise().root() / "ducktape/MODULE.bazel")).parent


@pytest.fixture(scope="session")
def k8s_dir(repo_root: Path) -> Path:
    return repo_root / "cluster/k8s"
