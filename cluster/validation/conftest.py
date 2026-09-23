"""Shared fixtures for cluster validation tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from more_itertools import one

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


@pytest.fixture(scope="session")
def clickhouse_installation(k8s_dir: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        one(
            manifest
            for manifest in yaml.safe_load_all((k8s_dir / "clickhouse/cluster/clickhouse.k8s.yaml").read_text())
            if manifest["kind"] == "ClickHouseInstallation"
        ),
    )


@pytest.fixture(scope="session")
def clickhouse_host(clickhouse_installation: dict[str, Any]) -> str:
    return ".".join(
        [
            clickhouse_installation["metadata"]["name"],
            clickhouse_installation["metadata"]["namespace"],
            "svc",
            "cluster.local",
        ]
    )
