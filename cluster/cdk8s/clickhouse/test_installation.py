"""The finance reader's generated credential and database grants stay separate."""

import pytest_bazel
from cdk8s import Testing as CdkTesting

from cluster.cdk8s.clickhouse import client, installation


def test_finance_reader_is_minted_and_scoped_to_typed_quota_history() -> None:
    docs = CdkTesting.synth(installation.clickhouse_chart(CdkTesting.app()))
    user = client.FINANCE_AGENT_USER
    chi = next(doc for doc in docs if doc["kind"] == "ClickHouseInstallation")
    users = chi["spec"]["configuration"]["users"]
    assert users[f"{user}/password"] == {
        "valueFrom": {"secretKeyRef": {"name": client.FINANCE_AGENT_CREDENTIALS, "key": client.PASSWORD_KEY}}
    }
    assert users[f"{user}/profile"] == "readonly"
    assert users[f"{user}/quota"] == "readonly"
    assert users[f"{user}/grants/query"] == ["GRANT SELECT ON aiquota.aiquota_windows"]
    generator = next(doc for doc in docs if doc["kind"] == "Password")
    assert generator["metadata"]["name"] == client.FINANCE_AGENT_CREDENTIALS
    secret = next(doc for doc in docs if doc["kind"] == "ExternalSecret")
    assert secret["metadata"]["name"] == client.FINANCE_AGENT_CREDENTIALS
    assert secret["spec"]["refreshPolicy"] == "CreatedOnce"
    assert "password" in secret["spec"]["target"]["template"]["data"]


if __name__ == "__main__":
    pytest_bazel.main()
