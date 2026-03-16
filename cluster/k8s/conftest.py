"""Shared fixtures for cluster validation integration tests.

Pure-analysis tests resolve cluster/k8s/ from runfiles (data deps).
Genrule-dependent tests (kustomize, flux) load pre-built results directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cluster.validation.cluster import ParsedCluster, parse_cluster
from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"
_FLUX_BUILD_RESULTS_RLOCATION = "_main/cluster/validation/flux_build_results.yaml"


@pytest.fixture(scope="session")
def workspace() -> Path:
    """Repo root within runfiles (parent of cluster/k8s)."""
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent.parent.parent


@pytest.fixture(scope="session")
def k8s_dir(workspace: Path) -> Path:
    return workspace / "cluster" / "k8s"


@pytest.fixture(scope="session")
def cluster(k8s_dir: Path) -> ParsedCluster:
    return parse_cluster(k8s_dir)


@pytest.fixture(scope="session")
def flux_build_output() -> str:
    """Load pre-built flux build output from genrule."""
    return get_required_path(_FLUX_BUILD_RESULTS_RLOCATION).read_text()
