"""Shared fixtures for cluster validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


@pytest.fixture(scope="session")
def repo_root(k8s_dir: Path) -> Path:
    """The runfiles checkout both manifest roots sit in (cluster/cdk8s/manifest_roots.py)."""
    return k8s_dir.parents[1]


@pytest.fixture(scope="session")
def generated_dir(repo_root: Path) -> Path:
    return repo_root / GENERATED_ROOT
