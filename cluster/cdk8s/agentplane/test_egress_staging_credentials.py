"""Staging credential sources, their readers, and their egress policies."""

from collections.abc import Callable
from typing import Any

import pytest
import pytest_bazel
from cdk8s import App, Chart, Testing as Cdk8sTesting

from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac
from cluster.cdk8s.agentplane.egress_credentials import STAGING_CREDENTIALS_NAMESPACE
from cluster.cdk8s.agentplane.egress_staging_credentials import add_staging_egress_credentials
from cluster.cdk8s.clickhouse import client as clickhouse
from cluster.cdk8s.testing.manifests import find_resource


@pytest.fixture(scope="module")
def credentials(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    chart = Chart(scratch_app_factory(), "credentials")
    add_staging_egress_credentials(
        chart, namespace="agentplane-staging", credentials_namespace=STAGING_CREDENTIALS_NAMESPACE
    )
    return Cdk8sTesting.synth(chart)


@pytest.mark.parametrize(
    ("credential", "source_namespace", "secret", "key"),
    [
        ("forgejo-haku", "haku-sandbox", "haku-forgejo-git", "password"),
        (
            clickhouse.AIQUOTA_HISTORY_CREDENTIALS,
            clickhouse.NAMESPACE,
            clickhouse.AIQUOTA_HISTORY_CREDENTIALS,
            clickhouse.PASSWORD_KEY,
        ),
    ],
)
def test_credential_copy_uses_a_scoped_source_reader(
    credentials: list[dict[str, Any]], credential: str, source_namespace: str, secret: str, key: str
) -> None:
    source = find_resource(credentials, "EgressCredential", credential)["spec"]["source"]["secretRef"]
    copy = find_resource(credentials, "ExternalSecret", source["name"], namespace=STAGING_CREDENTIALS_NAMESPACE)["spec"]
    assert copy["target"].get("name", source["name"]) == source["name"]
    assert copy["data"] == [{"secretKey": source["key"], "remoteRef": {"key": secret, "property": key}}]
    ref = copy["secretStoreRef"]
    store = find_resource(credentials, ref["kind"], ref["name"])["spec"]
    assert store["conditions"] == [{"namespaces": [STAGING_CREDENTIALS_NAMESPACE]}]
    provider = store["provider"]["kubernetes"]
    assert provider["remoteNamespace"] == source_namespace
    assert provider["auth"]["serviceAccount"] == {"name": "external-creds-reader"}

    rbac = Rbac(credentials)
    read = Permission(source_namespace, "", "secrets", "get", secret)
    readers: set[tuple[str, str | None, str]] = set()
    for binding in rbac.bindings:
        for grant in rbac.binding_rules(binding):
            if not grant.covers(Permission(source_namespace, "", "secrets", grant.verb, secret)):
                continue
            assert grant == read
            readers.update((s["kind"], s.get("namespace"), s["name"]) for s in binding["subjects"])
    assert readers == {("ServiceAccount", STAGING_CREDENTIALS_NAMESPACE, provider["auth"]["serviceAccount"]["name"])}


def test_thrive_read_has_scoped_bearer_and_get_rules(credentials: list[dict[str, Any]]) -> None:
    credential = find_resource(credentials, "EgressCredential", "thrive-read")
    assert credential["spec"]["source"] == {
        "secretRef": {"name": "platform-sessions-thrive-read-bearer", "key": "token"}
    }
    assert credential["spec"]["targets"] == [{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}]
    policy = find_resource(credentials, "PolicySet", "thrive-read")
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


def test_quest_bearer_has_its_own_secret_and_network_access(credentials: list[dict[str, Any]]) -> None:
    credential = find_resource(credentials, "EgressCredential", "quest-scheduler-read")
    assert credential["spec"]["source"] == {
        "secretRef": {"name": "platform-sessions-quest-scheduler-bearer", "key": "token"}
    }
    assert credential["spec"]["targets"] == [{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}]
    policy = find_resource(credentials, "PolicySet", "quest-scheduler-read")
    rules = policy["spec"]["rules"]
    assert [rule["methods"] for rule in rules] == [["GET"], ["POST"]]
    assert all(rule["hosts"] == ["platform-sessions.allegedly.works"] for rule in rules)
    assert all(rule["paths"] for rule in rules)
    assert all(path.startswith("/proxy/quest-scheduler/") for rule in rules for path in rule["paths"])
    network = find_resource(credentials, "CiliumNetworkPolicy", "egress-staging-credentials")
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
