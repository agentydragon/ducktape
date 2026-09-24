"""The hand-written sides of public-coder's mediated ClickHouse reader: the SOPS Secret holding
its password, and the iron config that substitutes it for the app's placeholder. The
constructs share `cluster.cdk8s.clickhouse.client`'s account, Secret and address."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import public_coder_agent_config, public_coder_proxy
from cluster.cdk8s.clickhouse import client


@pytest.fixture
def source_secret(k8s_dir: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/cluster/public-coder-credentials.sops.yaml").read_text())
    )


@pytest.fixture
def app() -> dict[str, Any]:
    objects = cast(list[dict[str, Any]], Cdk8sTesting.synth(public_coder_agent_config.app_chart(Cdk8sTesting.app())))
    return one(obj for obj in objects if obj["kind"] == "Deployment")


@pytest.fixture
def iron(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "agents/public-coder-agent/proxy/iron.yaml").read_text()))


def test_public_coder_clickhouse_reader_contract(
    source_secret: dict[str, Any], app: dict[str, Any], iron: dict[str, Any]
) -> None:
    """The Console-managed public-coder runner gets a mediated native ClickHouse reader: the
    app has only a placeholder, while the real password is reflected into its Iron proxy's
    namespace."""
    metadata = source_secret["metadata"]
    assert metadata["name"] == client.PUBLIC_CODER_CREDENTIALS
    assert metadata["namespace"] == client.NAMESPACE
    assert client.PASSWORD_KEY in source_secret["stringData"]
    annotations = metadata["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == public_coder_proxy.NAMESPACE
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == public_coder_proxy.NAMESPACE

    secrets = one(transform for transform in iron["transforms"] if transform["name"] == "secrets")["config"]["secrets"]
    clickhouse_secret = one(
        secret for secret in secrets if secret["source"]["var"] == "CLICKHOUSE_PUBLIC_CODER_PASSWORD"
    )
    assert clickhouse_secret["replace"]["match_headers"] == ["Authorization"]
    assert clickhouse_secret["rules"] == [{"host": client.HOST}]

    app_env = {
        entry["name"]: entry["value"]
        for entry in app["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in entry
    }
    assert app_env["CLICKHOUSE_PUBLIC_CODER_PASSWORD"] == clickhouse_secret["replace"]["proxy_value"]


if __name__ == "__main__":
    pytest_bazel.main()
