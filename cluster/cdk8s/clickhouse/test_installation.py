"""The AIQuota history reader's generated credential and database grants stay separate."""

from collections.abc import Callable
from typing import Any

import pytest
import pytest_bazel
from cdk8s import App

from cluster.cdk8s.clickhouse import client, installation
from cluster.cdk8s.testing.manifests import find_resource


@pytest.fixture(scope="module")
def installation_docs(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    return installation.clickhouse_chart(scratch_app_factory()).to_json()


@pytest.fixture(scope="module")
def chi(installation_docs: list[dict[str, Any]]) -> dict[str, Any]:
    return find_resource(installation_docs, "ClickHouseInstallation", client.NAME)


def test_aiquota_history_reader_is_minted_and_scoped_to_typed_quota_history(
    installation_docs: list[dict[str, Any]], chi: dict[str, Any]
) -> None:
    user = client.AIQUOTA_HISTORY_USER
    users = chi["spec"]["configuration"]["users"]
    assert users[f"{user}/profile"] == "readonly"
    assert users[f"{user}/quota"] == "readonly"
    assert users[f"{user}/grants/query"] == [
        "GRANT SELECT ON aiquota.aiquota_windows",
        "GRANT SELECT ON aiquota.token_activity_daily",
        "GRANT SELECT ON aiquota.reset_credits",
    ]
    secret = find_resource(installation_docs, "ExternalSecret", client.AIQUOTA_HISTORY_CREDENTIALS)
    assert secret["spec"]["refreshPolicy"] == "CreatedOnce"
    target = secret["spec"]["target"]
    assert target["name"] == client.AIQUOTA_HISTORY_CREDENTIALS
    assert users[f"{user}/password"] == {
        "valueFrom": {"secretKeyRef": {"name": target["name"], "key": client.PASSWORD_KEY}}
    }
    assert client.PASSWORD_KEY in target["template"]["data"]
    [source] = secret["spec"]["dataFrom"]
    generator = source["sourceRef"]["generatorRef"]
    assert generator["kind"] == "Password"
    find_resource(installation_docs, generator["kind"], generator["name"])


def test_shared_services_use_ready_installation_replicas(scratch_app: App, chi: dict[str, Any]) -> None:
    service = find_resource(installation.service_chart(scratch_app).to_json(), "Service", client.HTTP.name)
    assert "serviceTemplate" not in chi["spec"]["defaults"]["templates"]
    assert service["spec"]["selector"] == client.HTTP.pods.selector
    assert service["spec"]["selector"]["clickhouse.altinity.com/ready"] == "yes"


def test_retiring_full_node_preserves_surviving_host_and_replica_names(chi: dict[str, Any]) -> None:
    layout = chi["spec"]["configuration"]["clusters"][0]["layout"]
    assert [replica["name"] for replica in layout["replicas"]] == ["1", "2"]
    hosts = layout["shards"][0]["replicas"]
    pod_templates = {template["name"]: template["spec"] for template in chi["spec"]["templates"]["podTemplates"]}
    assert [
        (host["name"], pod_templates[host["templates"]["podTemplate"]]["nodeSelector"]["kubernetes.io/hostname"])
        for host in hosts
    ] == [("0-1", "ovh-ns103656"), ("0-3", "ovh-ns102453")]
    assert all(host["templates"]["dataVolumeClaimTemplate"] == "data-piraeus-hdd" for host in hosts)


if __name__ == "__main__":
    pytest_bazel.main()
