"""Access boundaries of the parked public-coder app."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one

from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.public_coder import app as public_coder_app, egress as public_coder_egress


def _one(objects: list[dict[str, Any]], kind: str, name: str | None = None) -> dict[str, Any]:
    return one(obj for obj in objects if obj["kind"] == kind and name in {None, obj["metadata"]["name"]})


@pytest.fixture(scope="module")
def app_objects(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(public_coder_app.app_chart(scratch_app_factory())))


def test_app_egress_reaches_the_internet_only_through_the_proxy(app_objects: list[dict[str, Any]]) -> None:
    egress = _one(app_objects, "NetworkPolicy", "egress")["spec"]["egress"]
    assert all(rule.get("to") for rule in egress)
    assert not any("ipBlock" in peer for rule in egress for peer in rule["to"])
    assert not {port["port"] for rule in egress for port in rule.get("ports", [])} & {443, 6443}


def test_app_reaches_clickhouse_only_through_the_proxy(app_objects: list[dict[str, Any]]) -> None:
    """ClickHouse stays out of NO_PROXY: only the proxy replaces the app's password placeholder."""
    container = one(
        c for c in _one(app_objects, "Deployment")["spec"]["template"]["spec"]["containers"] if c["name"] == "openclaw"
    )
    no_proxy = one(entry["value"] for entry in container["env"] if entry["name"] == "NO_PROXY").split(",")
    assert not {client.HTTP.fqdn, client.HTTP.host} & set(no_proxy)


def test_openclaw_uses_relay_without_receiving_its_svid(app_objects: list[dict[str, Any]]) -> None:
    pod = _one(app_objects, "Deployment")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "openclaw"
    assert pod["automountServiceAccountToken"] is False
    account = _one(app_objects, "ServiceAccount", "openclaw")
    assert account["automountServiceAccountToken"] is False
    relay = one(c for c in pod["containers"] if c["name"] == "egress-sidecar")
    assert public_coder_egress.SVID_VOLUME in {v["name"] for v in relay["volumeMounts"]}
    for container in pod["initContainers"] + [c for c in pod["containers"] if c != relay]:
        assert public_coder_egress.SVID_VOLUME not in {v["name"] for v in container["volumeMounts"]}


def test_openclaw_cannot_bypass_agentplane_egress(app_objects: list[dict[str, Any]]) -> None:
    rules = _one(app_objects, "NetworkPolicy", "egress")["spec"]["egress"]
    assert {p["port"] for r in rules for p in r["ports"]} == {53, 8888, 4000}
    gateway = one(r for r in rules if {p["port"] for p in r["ports"]} == {8888})
    assert gateway["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "agentplane-staging"}},
            "podSelector": {"matchLabels": public_coder_egress.GATEWAY.pods.selector},
        }
    ]


def test_rules_route_wires_workload_identity_without_mounting_the_svid_into_openclaw(
    app_objects: list[dict[str, Any]],
) -> None:
    settings = yaml.safe_load(_one(app_objects, "ConfigMap", "openclaw-egress-sidecar")["data"]["settings.yaml"])
    route = settings["platform_routes"]
    route_config = route["routes"]["egress-rules"]
    assert route["listen_address"] == "127.0.0.1:3130"
    assert route_config["service_prefixes"] == ["/agentplane.egress.EgressRules/"]
    assert route_config["upstream"] == "egress-admin.agentplane-staging.svc.cluster.local:8081"
    assert route_config["transport"] == {"mode": "tls_connect", "server_uri": "spiffe://agentplane/egress-proxy"}
    pod = _one(app_objects, "Deployment", "public-coder-agent")["spec"]["template"]["spec"]
    app = one(c for c in pod["containers"] if c["name"] == "openclaw")
    env = {item["name"]: item.get("value") for item in app["env"]}
    assert env["AGENTPLANE_EGRESS_GRPC_URL"] == "http://127.0.0.1:3130"
    assert "AGENTPLANE_EGRESS_GRPC_CREDENTIAL" not in env
    assert public_coder_egress.SVID_VOLUME not in {mount["name"] for mount in app["volumeMounts"]}


if __name__ == "__main__":
    pytest_bazel.main()
