"""Shared fixtures for cluster validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from python.runfiles import Runfiles

from cluster.cdk8s.manifest_roots import GENERATED_ROOT


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The materialized runfiles checkout, anchored by its root MODULE.bazel."""
    return Path(str(Runfiles.CreateOrRaise().root() / "ducktape/MODULE.bazel")).parent


@pytest.fixture(scope="session")
def k8s_dir(repo_root: Path) -> Path:
    return repo_root / "cluster/k8s"


@pytest.fixture(scope="session")
def generated_dir(repo_root: Path) -> Path:
    return repo_root / GENERATED_ROOT
