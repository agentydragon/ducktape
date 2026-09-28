"""The hand-written side of public-coder's mediated ClickHouse reader: the SOPS Secret holding
its password, which Reflector mirrors into the Iron proxy's namespace. The constructs share
`cluster.cdk8s.clickhouse.client`'s account, Secret and address."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
import yaml

from cluster.cdk8s import public_coder_proxy
from cluster.cdk8s.clickhouse import client


@pytest.fixture
def source_secret(k8s_dir: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/cluster/public-coder-credentials.sops.yaml").read_text())
    )


def test_public_coder_clickhouse_reader_contract(source_secret: dict[str, Any]) -> None:
    """The real password is reflected into the Iron proxy's namespace."""
    metadata = source_secret["metadata"]
    assert metadata["name"] == client.PUBLIC_CODER_CREDENTIALS
    assert metadata["namespace"] == client.NAMESPACE
    assert client.PASSWORD_KEY in source_secret["stringData"]
    annotations = metadata["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == public_coder_proxy.NAMESPACE
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == public_coder_proxy.NAMESPACE


if __name__ == "__main__":
    pytest_bazel.main()
