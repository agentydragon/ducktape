"""Direct-file contract for public-coder's mediated ClickHouse reader."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def test_public_coder_clickhouse_reader_contract(k8s_dir: Path) -> None:
    """The Console-managed public-coder runner gets a mediated native ClickHouse reader.

    The app has only a placeholder, while the real password is reflected into
    its Iron proxy. Both ClickHouse and Cilium constrain the resulting query
    surface; 8123 remains an internal ClusterIP port, not a Gateway route.
    """
    clickhouse_dir = k8s_dir / "clickhouse" / "cluster"
    agent_dir = k8s_dir / "agents" / "public-coder-agent"

    installation = yaml.safe_load((clickhouse_dir / "clickhouse.yaml").read_text())
    users = installation["spec"]["configuration"]["users"]
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

    source_secret = yaml.safe_load((clickhouse_dir / "public-coder-credentials.sops.yaml").read_text())
    annotations = source_secret["metadata"]["annotations"]
    assert source_secret["metadata"]["namespace"] == "clickhouse"
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == "public-coder-agent"
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == "public-coder-agent"

    app = yaml.safe_load((agent_dir / "app/deployment.yaml").read_text())
    app_env = {
        entry["name"]: entry["value"]
        for entry in app["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in entry
    }
    assert app_env["CLICKHOUSE_PUBLIC_CODER_USER"] == "public_coder_analytics"
    assert app_env["CLICKHOUSE_PUBLIC_CODER_PASSWORD"] == "proxy-clickhouse-public-coder-password"
    assert app_env["NO_PROXY"] == "127.0.0.1,localhost,litellm.litellm.svc,litellm.litellm.svc.cluster.local"

    proxy = yaml.safe_load((agent_dir / "proxy/deployment.yaml").read_text())
    proxy_env = {entry["name"]: entry for entry in proxy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert proxy_env["CLICKHOUSE_PUBLIC_CODER_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "clickhouse-public-coder-credentials",
        "key": "password",
    }

    iron = yaml.safe_load((agent_dir / "proxy/iron.yaml").read_text())
    secrets = next(transform for transform in iron["transforms"] if transform["name"] == "secrets")["config"]["secrets"]
    clickhouse_secret = next(
        secret for secret in secrets if secret["source"]["var"] == "CLICKHOUSE_PUBLIC_CODER_PASSWORD"
    )
    assert clickhouse_secret["replace"] == {
        "proxy_value": "proxy-clickhouse-public-coder-password",
        "match_headers": ["Authorization"],
    }
    assert clickhouse_secret["rules"] == [{"host": "clickhouse.clickhouse.svc.cluster.local"}]

    policies = list(yaml.safe_load_all((clickhouse_dir / "networkpolicy.yaml").read_text()))
    clickhouse_ingress = next(policy for policy in policies if policy["metadata"]["name"] == "clickhouse-ingress")
    clickhouse_rule = next(
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

    proxy_egress = yaml.safe_load((agent_dir / "proxy/cnp-egress.yaml").read_text())
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
