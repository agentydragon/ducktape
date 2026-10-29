"""Staging's Forgejo credential reaches its credentials namespace through exact source access."""

from pathlib import Path

import pytest_bazel
from cdk8s import Chart, Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import STAGING_CREDENTIALS_NAMESPACE
from cluster.cdk8s.agentplane.egress_staging_credentials import add_staging_egress_credentials
from cluster.cdk8s.clickhouse import client as clickhouse
from cluster.cdk8s.testing.scratch import scratch_chart


def _staging_credentials_resources(chart: Chart) -> list[dict]:
    add_staging_egress_credentials(
        chart, namespace="agentplane-staging", credentials_namespace=STAGING_CREDENTIALS_NAMESPACE
    )
    return Cdk8sTesting.synth(chart)


def test_forgejo_password_has_one_reader_and_exact_source_access(tmp_path: Path) -> None:
    objects = _staging_credentials_resources(scratch_chart(tmp_path))
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


def test_clickhouse_history_password_has_one_exact_source_reader(tmp_path: Path) -> None:
    objects = _staging_credentials_resources(scratch_chart(tmp_path))
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


if __name__ == "__main__":
    pytest_bazel.main()
