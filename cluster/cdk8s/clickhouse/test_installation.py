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


def test_shared_services_use_ready_installation_replicas(scratch_app: App) -> None:
    chi = next(
        doc
        for doc in CdkTesting.synth(installation.clickhouse_chart(scratch_app))
        if doc["kind"] == "ClickHouseInstallation"
    )
    service = next(doc for doc in CdkTesting.synth(installation.service_chart(scratch_app)) if doc["kind"] == "Service")
    assert "serviceTemplate" not in chi["spec"]["defaults"]["templates"]
    assert service["spec"]["selector"] == client.HTTP.pods.selector
    assert service["spec"]["selector"]["clickhouse.altinity.com/ready"] == "yes"


def test_retiring_full_node_preserves_surviving_host_and_replica_names(scratch_app: App) -> None:
    docs = CdkTesting.synth(installation.clickhouse_chart(scratch_app))
    chi = next(doc for doc in docs if doc["kind"] == "ClickHouseInstallation")
    layout = chi["spec"]["configuration"]["clusters"][0]["layout"]
    assert layout["replicasCount"] == 2
    assert [replica["name"] for replica in layout["replicas"]] == ["1", "2"]
    hosts = layout["shards"][0]["replicas"]
    assert [(host["name"], host["templates"]["podTemplate"]) for host in hosts] == [
        ("0-1", "clickhouse-piraeus-103656"),
        ("0-3", "clickhouse-piraeus-102453"),
    ]
    assert all(host["templates"]["dataVolumeClaimTemplate"] == "data-piraeus-hdd" for host in hosts)


if __name__ == "__main__":
    pytest_bazel.main()
