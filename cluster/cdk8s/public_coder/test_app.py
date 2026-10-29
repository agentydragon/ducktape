"""public-coder's access boundaries, across the charts that grant or carry them.

The profile's RBAC is spread over the rbac-base, Haku console, ClickHouse diagnostics,
ducktape-flux and public-coder rbac charts; its traffic over the app and Agentplane egress.
The ClickHouse reader's hand-written Secret is checked in
`//cluster/validation:test_public_coder_clickhouse_reader_contract`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import (
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one

from cluster.cdk8s import agent_rbac_base, agent_shared_rbac
from cluster.cdk8s.clickhouse import client, installation
from cluster.cdk8s.haku.charts import console_chart
from cluster.cdk8s.public_coder import app as public_coder_app, egress as public_coder_egress
from cluster.cdk8s.testing.scratch import scratch_app

_HAKU_SUBJECTS = {
    ("Group", "oidc-ksbx-groups:haku", None),
    ("Group", "haku:access-profile:haku", None),
    ("ServiceAccount", "haku", "haku-sandbox"),
}
_READ = ["get", "list", "watch"]


def _synth(chart: Chart) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))


def _subjects(binding: dict[str, Any]) -> set[tuple[str, str, str | None]]:
    return {(item["kind"], item["name"], item.get("namespace")) for item in binding["subjects"]}


def _resources(role: dict[str, Any]) -> set[str]:
    return set().union(*(set(rule["resources"]) for rule in role["rules"]))


def _named(objects: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [obj for obj in objects if obj["metadata"]["name"] == name]


def _one(objects: list[dict[str, Any]], kind: str, name: str | None = None) -> dict[str, Any]:
    return one(obj for obj in objects if obj["kind"] == kind and name in {None, obj["metadata"]["name"]})


@pytest.fixture(scope="module")
def app_objects(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    return _synth(public_coder_app.app_chart(scratch_app(tmp_path_factory.mktemp("cdk8s"))))


@pytest.fixture(scope="module")
def rbac_objects(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    return _synth(public_coder_app.rbac_chart(scratch_app(tmp_path_factory.mktemp("cdk8s"))))


@pytest.fixture(scope="module")
def console_objects(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    return _synth(console_chart(scratch_app(tmp_path_factory.mktemp("cdk8s"))))


@pytest.fixture(scope="module")
def clickhouse_diagnostics_objects(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    return _synth(installation.agent_diagnostics_rbac_chart(scratch_app(tmp_path_factory.mktemp("cdk8s"))))


def test_public_coder_and_haku_configured_diagnostics_are_secret_free(
    tmp_path: Path,
    console_objects: list[dict[str, Any]],
    rbac_objects: list[dict[str, Any]],
    clickhouse_diagnostics_objects: list[dict[str, Any]],
) -> None:
    """Configured public diagnostics do not widen secret or exec access."""
    metadata_role = _one(
        _synth(agent_rbac_base.chart(scratch_app(tmp_path))), "ClusterRole", "agent-readable-namespace-metadata"
    )
    assert all(rule["verbs"] == _READ for rule in metadata_role["rules"])
    assert not {
        "secrets",
        "externalsecrets",
        "secretstores",
        "clustersecretstores",
        "pushsecrets",
        "clusterpushsecrets",
        "pods/log",
        "pods/exec",
        "pods/attach",
        "pods/portforward",
    } & _resources(metadata_role)

    sources = {
        "clickhouse agent-diagnostics-rbac chart": clickhouse_diagnostics_objects,
        "haku-console chart": _named(console_objects, "agent-haku-console-metadata-reader"),
        "public-coder-agent chart": _named(rbac_objects, "agent-public-coder-extended-diagnostics-reader"),
    }
    for source, objects in sources.items():
        role = _one(objects, "Role")
        binding = _one(objects, "RoleBinding")
        assert binding["roleRef"]["name"] == role["metadata"]["name"], source
        assert all(rule["verbs"] == _READ for rule in role["rules"]), source
        assert not {"secrets", "pods/log", "pods/exec"} & _resources(role), source

    # The staging public-coder preset receives cluster diagnostics through its Agentplane grant.
    shared_objects = _synth(agent_shared_rbac.chart(scratch_app(tmp_path)))
    haku_cluster_binding = _one(shared_objects, "ClusterRoleBinding", "agent-cluster-diagnostics-reader")
    assert _subjects(haku_cluster_binding) >= _HAKU_SUBJECTS

    diagnostics_role = _one(
        _synth(agent_rbac_base.chart(scratch_app(tmp_path))), "ClusterRole", "cluster-diagnostics-reader"
    )
    assert {
        (api_group, resource, verb)
        for rule in diagnostics_role["rules"]
        for api_group in rule["apiGroups"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
    } >= {("redis.redis.opstreelabs.in", "redisreplications", verb) for verb in _READ}


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


def test_openclaw_uses_relay_without_receiving_its_token(app_objects: list[dict[str, Any]]) -> None:
    pod = _one(app_objects, "Deployment")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "openclaw"
    assert pod["automountServiceAccountToken"] is False
    account = _one(app_objects, "ServiceAccount", "openclaw")
    assert account["automountServiceAccountToken"] is False
    relay = one(c for c in pod["containers"] if c["name"] == "egress-sidecar")
    assert "agentplane-egress-token" in {v["name"] for v in relay["volumeMounts"]}
    for container in pod["initContainers"] + [c for c in pod["containers"] if c != relay]:
        assert "agentplane-egress-token" not in {v["name"] for v in container["volumeMounts"]}


def test_openclaw_cannot_bypass_agentplane_egress(app_objects: list[dict[str, Any]]) -> None:
    rules = _one(app_objects, "NetworkPolicy", "egress")["spec"]["egress"]
    assert {p["port"] for r in rules for p in r["ports"]} == {53, 8888, 4000}
    gateway = one(r for r in rules if r["ports"] == [{"port": 8888, "protocol": "TCP"}])
    assert gateway["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "agentplane-staging"}},
            "podSelector": {"matchLabels": public_coder_egress.GATEWAY.pods.selector},
        }
    ]


if __name__ == "__main__":
    pytest_bazel.main()
