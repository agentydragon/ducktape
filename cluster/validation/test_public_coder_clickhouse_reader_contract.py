"""Direct-file contract for public-coder's mediated ClickHouse reader."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import public_coder_agent_config


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
    clickhouse_installation: dict[str, Any],
    clickhouse_host: str,
    source_secret: dict[str, Any],
    app: dict[str, Any],
    iron: dict[str, Any],
) -> None:
    """The Console-managed public-coder runner gets a mediated native ClickHouse reader.

    The app has only a placeholder, while the real password is reflected into
    its Iron proxy; the exact grant list is the read-only data boundary. The
    proxy-side wiring, which ClickHouse and Cilium constrain to the internal
    8123 port, is `//cluster/cdk8s:test_public_coder_proxy`.
    """
    users = clickhouse_installation["spec"]["configuration"]["users"]
    clickhouse_credentials_ref = users["public_coder_analytics/password"]["valueFrom"]["secretKeyRef"]
    assert clickhouse_credentials_ref["key"] == "password"

    annotations = source_secret["metadata"]["annotations"]
    assert source_secret["metadata"]["name"] == clickhouse_credentials_ref["name"]
    assert source_secret["metadata"]["namespace"] == clickhouse_installation["metadata"]["namespace"]
    assert clickhouse_credentials_ref["key"] in source_secret["stringData"]

    agent_namespace = app["metadata"]["namespace"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == agent_namespace
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == agent_namespace

    secrets = one(transform for transform in iron["transforms"] if transform["name"] == "secrets")["config"]["secrets"]
    clickhouse_secret = one(
        secret for secret in secrets if secret["source"]["var"] == "CLICKHOUSE_PUBLIC_CODER_PASSWORD"
    )
    assert clickhouse_secret["replace"]["match_headers"] == ["Authorization"]
    assert clickhouse_secret["rules"] == [{"host": clickhouse_host}]

    app_env = {
        entry["name"]: entry["value"]
        for entry in app["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in entry
    }
    assert app_env["CLICKHOUSE_PUBLIC_CODER_PASSWORD"] == clickhouse_secret["replace"]["proxy_value"]
    assert "clickhouse.clickhouse.svc" not in app_env["NO_PROXY"].split(",")


if __name__ == "__main__":
    pytest_bazel.main()
