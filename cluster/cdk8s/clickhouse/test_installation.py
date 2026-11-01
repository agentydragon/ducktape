"""The AIQuota history reader's generated credential and database grants stay separate."""

import pytest_bazel
from cdk8s import App, Testing as CdkTesting

from cluster.cdk8s.clickhouse import client, installation


def test_aiquota_history_reader_is_minted_and_scoped_to_typed_quota_history(scratch_app: App) -> None:
    docs = CdkTesting.synth(installation.clickhouse_chart(scratch_app))
    user = client.AIQUOTA_HISTORY_USER
    chi = next(doc for doc in docs if doc["kind"] == "ClickHouseInstallation")
    users = chi["spec"]["configuration"]["users"]
    assert users[f"{user}/password"] == {
        "valueFrom": {"secretKeyRef": {"name": client.AIQUOTA_HISTORY_CREDENTIALS, "key": client.PASSWORD_KEY}}
    }
    assert users[f"{user}/profile"] == "readonly"
    assert users[f"{user}/quota"] == "readonly"
    assert users[f"{user}/grants/query"] == [
        "GRANT SELECT ON aiquota.aiquota_windows",
        "GRANT SELECT ON aiquota.token_activity_daily",
        "GRANT SELECT ON aiquota.reset_credits",
    ]
    generator = next(doc for doc in docs if doc["kind"] == "Password")
    assert generator["metadata"]["name"] == client.AIQUOTA_HISTORY_CREDENTIALS
    secret = next(doc for doc in docs if doc["kind"] == "ExternalSecret")
    assert secret["metadata"]["name"] == client.AIQUOTA_HISTORY_CREDENTIALS
    assert secret["spec"]["refreshPolicy"] == "CreatedOnce"
    assert "password" in secret["spec"]["target"]["template"]["data"]


def test_shared_client_services_select_the_same_replica_during_incident(scratch_app: App) -> None:
    chi_docs = CdkTesting.synth(installation.clickhouse_chart(scratch_app))
    service_docs = CdkTesting.synth(installation.service_chart(scratch_app))
    chi = next(doc for doc in chi_docs if doc["kind"] == "ClickHouseInstallation")
    client_service = next(doc for doc in service_docs if doc["kind"] == "Service")
    template_name = chi["spec"]["defaults"]["templates"]["serviceTemplate"]
    template = next(t for t in chi["spec"]["templates"]["serviceTemplates"] if t["name"] == template_name)
    gate = template["spec"]["selector"]
    assert gate == {"statefulset.kubernetes.io/pod-name": "chi-clickhouse-default-0-1-0"}
    assert client_service["spec"]["selector"].items() >= gate.items()
    assert template["spec"]["type"] == "ClusterIP"
    assert {port["name"] for port in template["spec"]["ports"]} == {"http", "tcp"}


if __name__ == "__main__":
    pytest_bazel.main()
