"""Direct-file contract for public-coder's mediated ClickHouse reader."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from more_itertools import one


@pytest.fixture
def clickhouse_installation(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/cluster/clickhouse.yaml").read_text()))


@pytest.fixture
def source_secret(k8s_dir: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/cluster/public-coder-credentials.sops.yaml").read_text())
    )


@pytest.fixture
def app(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "agents/public-coder-agent/app/deployment.yaml").read_text()))


@pytest.fixture
def proxy(k8s_dir: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any], yaml.safe_load((k8s_dir / "agents/public-coder-agent/proxy/deployment.yaml").read_text())
    )


@pytest.fixture
def iron(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "agents/public-coder-agent/proxy/iron.yaml").read_text()))


@pytest.fixture
def network_policies(k8s_dir: Path) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]], list(yaml.safe_load_all((k8s_dir / "clickhouse/cluster/networkpolicy.yaml").read_text()))
    )


@pytest.fixture
def proxy_egress(k8s_dir: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any], yaml.safe_load((k8s_dir / "agents/public-coder-agent/proxy/cnp-egress.yaml").read_text())
    )


def test_public_coder_clickhouse_reader_contract(
    clickhouse_installation: dict[str, Any],
    source_secret: dict[str, Any],
    app: dict[str, Any],
    proxy: dict[str, Any],
    iron: dict[str, Any],
    network_policies: list[dict[str, Any]],
    proxy_egress: dict[str, Any],
) -> None:
    """The Console-managed public-coder runner gets a mediated native ClickHouse reader.

    The app has only a placeholder, while the real password is reflected into
    its Iron proxy. Both ClickHouse and Cilium constrain the resulting query
    surface; 8123 remains an internal ClusterIP port, not a Gateway route.
    """
    users = clickhouse_installation["spec"]["configuration"]["users"]
    assert users["public_coder_analytics/password"]["valueFrom"]["secretKeyRef"] == {
        "name": "clickhouse-public-coder-credentials",
        "key": "password",
    }
    assert users["public_coder_analytics/profile"] == "readonly"
    assert users["public_coder_analytics/quota"] == "readonly"
    assert users["public_coder_analytics/grants/query"] == [
        "GRANT SELECT ON aiquota.aiquota_windows",
        "GRANT SELECT ON aiquota.raw_http_observations",
    ]

    annotations = source_secret["metadata"]["annotations"]
    assert source_secret["metadata"]["namespace"] == "clickhouse"
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == "public-coder-agent"
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == "public-coder-agent"

    app_env = {
        entry["name"]: entry["value"]
        for entry in app["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in entry
    }
    assert app_env["CLICKHOUSE_PUBLIC_CODER_USER"] == "public_coder_analytics"
    assert app_env["CLICKHOUSE_PUBLIC_CODER_PASSWORD"] == "proxy-clickhouse-public-coder-password"
    assert app_env["NO_PROXY"] == "127.0.0.1,localhost,litellm.litellm.svc,litellm.litellm.svc.cluster.local"

    proxy_password = one(
        entry
        for entry in proxy["spec"]["template"]["spec"]["containers"][0]["env"]
        if entry["name"] == "CLICKHOUSE_PUBLIC_CODER_PASSWORD"
    )
    assert proxy_password["valueFrom"]["secretKeyRef"] == {
        "name": "clickhouse-public-coder-credentials",
        "key": "password",
    }

    secrets = one(transform for transform in iron["transforms"] if transform["name"] == "secrets")["config"]["secrets"]
    clickhouse_secret = one(
        secret for secret in secrets if secret["source"]["var"] == "CLICKHOUSE_PUBLIC_CODER_PASSWORD"
    )
    assert clickhouse_secret["replace"] == {
        "proxy_value": "proxy-clickhouse-public-coder-password",
        "match_headers": ["Authorization"],
    }
    assert clickhouse_secret["rules"] == [{"host": "clickhouse.clickhouse.svc.cluster.local"}]

    clickhouse_ingress = one(
        policy for policy in network_policies if policy["metadata"]["name"] == "clickhouse-ingress"
    )
    clickhouse_rule = one(
        rule
        for rule in clickhouse_ingress["spec"]["ingress"]
        if rule.get("from")
        == [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "public-coder-agent"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "public-coder-agent-proxy"}},
            }
        ]
    )
    assert clickhouse_rule["ports"] == [{"port": 8123, "protocol": "TCP"}]

    assert {
        "toEndpoints": [
            {
                "matchLabels": {
                    "k8s:io.kubernetes.pod.namespace": "clickhouse",
                    "k8s:app.kubernetes.io/name": "clickhouse",
                    "k8s:app.kubernetes.io/instance": "clickhouse",
                }
            }
        ],
        "toPorts": [{"ports": [{"port": "8123", "protocol": "TCP"}]}],
    } in proxy_egress["spec"]["egress"]


if __name__ == "__main__":
    pytest_bazel.main()
