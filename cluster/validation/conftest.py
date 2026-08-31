"""Shared fixtures for cluster validation tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


@pytest.fixture(scope="session")
def clickhouse_installation(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/cluster/clickhouse.yaml").read_text()))


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
