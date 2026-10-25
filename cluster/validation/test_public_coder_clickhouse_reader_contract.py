"""The hand-written canonical Secret for public-coder's mediated ClickHouse reader.

Agentplane egress reads it through an exact-secret ExternalSecret store; it is not reflected into
the workload namespace. The constructs share `cluster.cdk8s.clickhouse.client`'s account,
Secret and address.
"""

from __future__ import annotations

from typing import Any, cast

import pytest_bazel
import yaml

from cluster.cdk8s.clickhouse import client
from util.bazel.runfiles import get_required_path


def test_public_coder_clickhouse_reader_contract() -> None:
    """The canonical credential stays in ClickHouse for Agentplane's exact-secret reader."""
    source_secret = cast(
        dict[str, Any],
        yaml.safe_load(
            get_required_path("_main/cluster/k8s/clickhouse/cluster/public-coder-credentials.sops.yaml").read_text()
        ),
    )
    metadata = source_secret["metadata"]
    assert metadata["name"] == client.PUBLIC_CODER_CREDENTIALS
    assert metadata["namespace"] == client.NAMESPACE
    assert client.PASSWORD_KEY in source_secret["stringData"]
    annotations = metadata.get("annotations", {})
    assert "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" not in annotations
    assert "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces" not in annotations


if __name__ == "__main__":
    pytest_bazel.main()
