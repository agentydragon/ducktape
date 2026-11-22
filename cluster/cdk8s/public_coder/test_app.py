"""Access boundaries of the parked public-coder app."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import App
from more_itertools import one

from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.public_coder import app as public_coder_app, egress as public_coder_egress
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


@pytest.fixture(scope="module")
def app_objects(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    return public_coder_app.app_chart(scratch_app_factory()).to_json()


@pytest.fixture(scope="module")
def pod(app_objects: list[dict[str, Any]]) -> dict[str, Any]:
    deployment = find_resource(app_objects, "Deployment", "public-coder-agent")
    return cast(dict[str, Any], deployment["spec"]["template"]["spec"])


def test_app_egress_reaches_the_internet_only_through_the_proxy(app_objects: list[dict[str, Any]]) -> None:
    egress = find_resource(app_objects, "NetworkPolicy", "egress")["spec"]["egress"]
    assert all(rule.get("to") for rule in egress)
    assert not any("ipBlock" in peer for rule in egress for peer in rule["to"])
    assert not {port["port"] for rule in egress for port in rule.get("ports", [])} & {443, 6443}


def test_app_reaches_clickhouse_only_through_the_proxy(pod: dict[str, Any]) -> None:
    """ClickHouse stays out of NO_PROXY: only the proxy replaces the app's password placeholder."""
    container = one(c for c in pod["containers"] if c["name"] == "openclaw")
    no_proxy = one(entry["value"] for entry in container["env"] if entry["name"] == "NO_PROXY").split(",")
    assert not {client.HTTP.fqdn, client.HTTP.host} & set(no_proxy)


def test_openclaw_uses_relay_without_receiving_its_svid(app_objects: list[dict[str, Any]], pod: dict[str, Any]) -> None:
    assert pod["serviceAccountName"] == "openclaw"
    assert pod["automountServiceAccountToken"] is False
    account = find_resource(app_objects, "ServiceAccount", "openclaw")
    assert account["automountServiceAccountToken"] is False
    svid = one(
        v["name"] for v in pod["volumes"] if v.get("secret", {}).get("secretName") == public_coder_egress.SVID_SECRET
    )
    assert [
        c["name"]
        for c in pod["initContainers"] + pod["containers"]
        if any(m["name"] == svid for m in c.get("volumeMounts", []))
    ] == ["egress-sidecar"]


def test_openclaw_cannot_bypass_agentplane_egress(app_objects: list[dict[str, Any]]) -> None:
    rules = find_resource(app_objects, "NetworkPolicy", "egress")["spec"]["egress"]
    assert {p["port"] for r in rules for p in r["ports"]} == {53, 8888, 4000}
    gateway = one(r for r in rules if {p["port"] for p in r["ports"]} == {8888})
    assert gateway["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "agentplane-staging"}},
            "podSelector": {"matchLabels": public_coder_egress.GATEWAY.pods.selector},
        }
    ]


def test_openclaw_can_query_only_rules_on_the_local_relay(
    app_objects: list[dict[str, Any]], pod: dict[str, Any]
) -> None:
    settings = config_map_yaml(app_objects, "openclaw-egress-sidecar", "settings.yaml")
    listener = settings["platform_routes"]
    assert set(listener["routes"]) == {"egress-rules"}
    assert listener["listen_address"].rsplit(":", 1)[0] == "127.0.0.1"
    app = one(c for c in pod["containers"] if c["name"] == "openclaw")
    env = {item["name"]: item.get("value") for item in app["env"]}
    assert env["AGENTPLANE_EGRESS_GRPC_URL"] == f"http://{listener['listen_address']}"
    assert "AGENTPLANE_EGRESS_GRPC_CREDENTIAL" not in env


if __name__ == "__main__":
    pytest_bazel.main()
