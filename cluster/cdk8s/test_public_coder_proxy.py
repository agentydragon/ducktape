"""public-coder's credential proxy is the one path from the agent to its ClickHouse reader.

The app holds only a placeholder; the proxy holds the reader's real password and is the only
cross-namespace client ClickHouse admits for it, on the internal HTTP port. The seams with
hand-written inputs (the SOPS source Secret, the iron config) stay in
`//cluster/validation:test_public_coder_clickhouse_reader_contract`.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import public_coder_agent_config, public_coder_proxy
from cluster.cdk8s.clickhouse import installation


@pytest.fixture(scope="module")
def clickhouse_installation() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        one(
            obj
            for obj in Cdk8sTesting.synth(installation.clickhouse_chart(Cdk8sTesting.app()))
            if obj["kind"] == "ClickHouseInstallation"
        ),
    )


@pytest.fixture(scope="module")
def app() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        one(
            obj
            for obj in Cdk8sTesting.synth(public_coder_agent_config.app_chart(Cdk8sTesting.app()))
            if obj["kind"] == "Deployment"
        ),
    )


@pytest.fixture(scope="module")
def proxy_objects() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(public_coder_proxy.chart(Cdk8sTesting.app())))


def test_public_coder_clickhouse_reader_goes_through_the_proxy(
    clickhouse_installation: dict[str, Any], app: dict[str, Any], proxy_objects: list[dict[str, Any]]
) -> None:
    clickhouse_namespace = clickhouse_installation["metadata"]["namespace"]
    users = clickhouse_installation["spec"]["configuration"]["users"]
    app_env = {
        entry["name"]: entry["value"]
        for entry in app["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in entry
    }
    clickhouse_credentials_ref = users[f"{app_env['CLICKHOUSE_PUBLIC_CODER_USER']}/password"]["valueFrom"][
        "secretKeyRef"
    ]

    proxy = one(obj for obj in proxy_objects if obj["kind"] == "Deployment")
    proxy_password = one(
        entry
        for entry in proxy["spec"]["template"]["spec"]["containers"][0]["env"]
        if entry["name"] == "CLICKHOUSE_PUBLIC_CODER_PASSWORD"
    )
    assert proxy_password["valueFrom"]["secretKeyRef"] == clickhouse_credentials_ref

    network_policies = Cdk8sTesting.synth(installation.networkpolicy_chart(Cdk8sTesting.app()))
    clickhouse_ingress = one(
        policy for policy in network_policies if policy["metadata"]["name"] == "clickhouse-ingress"
    )
    assert clickhouse_ingress["metadata"]["namespace"] == clickhouse_namespace
    proxy_pod_labels = proxy["spec"]["template"]["metadata"]["labels"]
    clickhouse_rule = one(
        rule
        for rule in clickhouse_ingress["spec"]["ingress"]
        if rule.get("from")
        == [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": app["metadata"]["namespace"]}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": proxy_pod_labels["app.kubernetes.io/name"]}},
            }
        ]
    )
    assert clickhouse_rule["ports"] == [{"port": 8123, "protocol": "TCP"}]

    proxy_egress = one(
        obj
        for obj in proxy_objects
        if obj["kind"] == "CiliumNetworkPolicy" and obj["metadata"]["name"] == "allow-public-coder-agent-proxy-egress"
    )
    clickhouse_pod_labels = one(
        template
        for template in clickhouse_installation["spec"]["templates"]["podTemplates"]
        if template["name"] == clickhouse_installation["spec"]["defaults"]["templates"]["podTemplate"]
    )["metadata"]["labels"]
    assert {
        "toEndpoints": [
            {
                "matchLabels": {
                    "k8s:io.kubernetes.pod.namespace": clickhouse_namespace,
                    **{
                        f"k8s:{key}": value
                        for key, value in clickhouse_pod_labels.items()
                        if key in {"app.kubernetes.io/name", "app.kubernetes.io/instance"}
                    },
                }
            }
        ],
        "toPorts": [{"ports": [{"port": "8123", "protocol": "TCP"}]}],
    } in proxy_egress["spec"]["egress"]


if __name__ == "__main__":
    pytest_bazel.main()
