"""Staging's Forgejo credential reaches its credentials namespace through exact source access."""

import pytest_bazel
from cdk8s import Chart, Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import STAGING_CREDENTIALS_NAMESPACE
from cluster.cdk8s.agentplane.egress_staging_credentials import add_staging_egress_credentials
from cluster.cdk8s.clickhouse import client as clickhouse


def _staging_credentials_resources(chart: Chart) -> list[dict]:
    add_staging_egress_credentials(
        chart,
        namespace="agentplane-staging",
        credentials_namespace=STAGING_CREDENTIALS_NAMESPACE,
        identity_catalog=False,
    )
    return Cdk8sTesting.synth(chart)


def test_forgejo_password_has_one_reader_and_exact_source_access(scratch_chart: Chart) -> None:
    objects = _staging_credentials_resources(scratch_chart)
    binding = next(obj for obj in objects if obj["kind"] == "RoleBinding")
    assert binding["subjects"] == [
        {
            "apiGroup": "",
            "kind": "ServiceAccount",
            "name": "external-creds-reader",
            "namespace": STAGING_CREDENTIALS_NAMESPACE,
        }
    ]

    source_role = next(
        obj for obj in objects if obj["kind"] == "Role" and obj["metadata"]["namespace"] == "haku-sandbox"
    )
    assert source_role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["haku-forgejo-git"], "verbs": ["get"]}
    ]
    store = next(obj for obj in objects if obj["kind"] == "ClusterSecretStore")
    assert store["spec"]["conditions"] == [{"namespaces": [STAGING_CREDENTIALS_NAMESPACE]}]
    provider = store["spec"]["provider"]["kubernetes"]
    assert provider["remoteNamespace"] == "haku-sandbox"
    assert provider["auth"]["serviceAccount"] == {"name": "external-creds-reader"}


def test_clickhouse_history_password_has_one_exact_source_reader(scratch_chart: Chart) -> None:
    objects = _staging_credentials_resources(scratch_chart)
    secret = clickhouse.AIQUOTA_HISTORY_CREDENTIALS
    role_name = "agentplane-staging-aiquota-history-clickhouse-reader"
    store_name = "kubernetes-agentplane-staging-aiquota-history-clickhouse-secret-store"

    source_role = next(
        resource
        for resource in objects
        if resource["kind"] == "Role"
        and resource["metadata"]["namespace"] == clickhouse.NAMESPACE
        and resource["metadata"]["name"] == role_name
    )
    assert source_role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": [secret], "verbs": ["get"]}
    ]

    source_binding = next(
        resource
        for resource in objects
        if resource["kind"] == "RoleBinding"
        and resource["metadata"]["namespace"] == clickhouse.NAMESPACE
        and resource["metadata"]["name"] == role_name
    )
    assert source_binding["roleRef"]["name"] == role_name
    assert source_binding["subjects"] == [
        {
            "apiGroup": "",
            "kind": "ServiceAccount",
            "name": "external-creds-reader",
            "namespace": STAGING_CREDENTIALS_NAMESPACE,
        }
    ]

    store = next(
        resource
        for resource in objects
        if resource["kind"] == "ClusterSecretStore" and resource["metadata"]["name"] == store_name
    )
    assert store["spec"]["conditions"] == [{"namespaces": [STAGING_CREDENTIALS_NAMESPACE]}]
    provider = store["spec"]["provider"]["kubernetes"]
    assert provider["remoteNamespace"] == clickhouse.NAMESPACE
    assert provider["auth"]["serviceAccount"] == {"name": "external-creds-reader"}

    external_secret = next(
        resource
        for resource in objects
        if resource["kind"] == "ExternalSecret"
        and resource["metadata"]["namespace"] == STAGING_CREDENTIALS_NAMESPACE
        and resource["metadata"]["name"] == secret
    )
    assert external_secret["spec"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": store_name}
    assert external_secret["spec"]["data"] == [
        {"secretKey": clickhouse.PASSWORD_KEY, "remoteRef": {"key": secret, "property": clickhouse.PASSWORD_KEY}}
    ]


def test_thrive_read_has_scoped_bearer_and_get_rules(scratch_chart: Chart) -> None:
    objects = _staging_credentials_resources(scratch_chart)
    credential = next(
        obj for obj in objects if obj["kind"] == "EgressCredential" and obj["metadata"]["name"] == "thrive-read"
    )
    assert credential["spec"]["source"] == {
        "secretRef": {"name": "platform-sessions-thrive-read-bearer", "key": "token"}
    }
    assert credential["spec"]["targets"] == [{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}]
    policy = next(obj for obj in objects if obj["kind"] == "PolicySet" and obj["metadata"]["name"] == "thrive-read")
    rules = policy["spec"]["rules"]
    assert [rule["methods"] for rule in rules] == [["GET"]]
    assert rules[0]["hosts"] == ["platform-sessions.allegedly.works"]
    assert set(rules[0]["paths"]) == {
        "/proxy/thrive/products",
        "/proxy/thrive/account/orders",
        "/proxy/thrive/account",
        "/proxy/thrive/carts",
        "/proxy/thrive/autoship/items",
        "/proxy/thrive/autoship/schedule/first",
    }


def test_quest_bearer_has_its_own_secret_and_network_access(scratch_chart: Chart) -> None:
    objects = _staging_credentials_resources(scratch_chart)
    credential = next(
        obj for obj in objects if obj["kind"] == "EgressCredential" and obj["metadata"]["name"] == "quest-read"
    )
    assert credential["spec"]["source"] == {"secretRef": {"name": "platform-sessions-quest-bearer", "key": "token"}}
    assert credential["spec"]["targets"] == [{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}]
    policy = next(obj for obj in objects if obj["kind"] == "PolicySet" and obj["metadata"]["name"] == "quest-read")
    rules = policy["spec"]["rules"]
    assert [rule["methods"] for rule in rules] == [["GET"], ["POST"]]
    assert all(rule["hosts"] == ["platform-sessions.allegedly.works"] for rule in rules)
    assert all(rule["paths"] for rule in rules)
    assert all(path.startswith("/proxy/quest/") for rule in rules for path in rule["paths"])
    network = next(
        obj
        for obj in objects
        if obj["kind"] == "CiliumNetworkPolicy" and obj["metadata"]["name"] == "egress-staging-credentials"
    )
    assert {
        "toEndpoints": [
            {
                "matchLabels": {
                    "app": "platform-sessions-gateway",
                    "k8s:io.kubernetes.pod.namespace": "platform-sessions",
                }
            }
        ],
        "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
    } in network["spec"]["egress"]


if __name__ == "__main__":
    pytest_bazel.main()
