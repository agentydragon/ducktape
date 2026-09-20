from typing import Any

import pytest
import pytest_bazel
from cdk8s import (
    ApiObject,
    ApiObjectMetadata,
    Chart,
    JsonPatch,
    Testing as Cdk8sTesting,
)  # pytest auto-collects classes named Test*

from cluster.cdk8s.fleet_rules import add_fleet_rules, pinned_https_egress, pod_hardening, resolved_references

_HARDENED = {"securityContext": {"seccompProfile": {"type": "RuntimeDefault"}}}
_RESOURCES = {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}}


def _deployment(name: str, *, containers: list[dict[str, Any]], pod: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {"template": {"spec": {"containers": containers, **(pod or {})}}},
    }


def _policy(name: str, egress: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": name},
        "spec": {"egress": egress},
    }


def _https(destination: dict[str, Any], server_names: list[str] | None = None) -> dict[str, Any]:
    to_ports: dict[str, Any] = {"ports": [{"port": "443", "protocol": "TCP"}]}
    if server_names is not None:
        to_ports["serverNames"] = server_names
    return {**destination, "toPorts": [to_ports]}


def test_pod_hardening_names_the_container_and_what_it_lacks() -> None:
    objects = [
        _deployment("bare", containers=[{"name": "main", "resources": {"requests": {"cpu": "1"}}}]),
        _deployment("ok", containers=[{"name": "main", "resources": _RESOURCES}], pod=_HARDENED),
        _deployment("own-profile", containers=[{"name": "main", "resources": _RESOURCES, **_HARDENED}]),
    ]
    assert pod_hardening(objects) == [
        "Deployment/bare container main: seccomp profile is None, not RuntimeDefault",
        "Deployment/bare container main: resources.requests lacks ['memory']",
        "Deployment/bare container main: resources.limits lacks ['memory']",
    ]


def test_pod_hardening_covers_init_containers_and_sandbox_templates() -> None:
    sandbox = {
        "apiVersion": "extensions.agents.x-k8s.io/v1alpha1",
        "kind": "SandboxTemplate",
        "metadata": {"name": "runner"},
        "spec": {"podTemplate": {"spec": {**_HARDENED, "containers": [{"name": "runner"}]}}},
    }
    deployment = _deployment(
        "with-init",
        containers=[{"name": "main", "resources": _RESOURCES}],
        pod={**_HARDENED, "initContainers": [{"name": "migrate"}]},
    )
    assert [error.split(":")[0] for error in pod_hardening([sandbox, deployment])] == [
        "SandboxTemplate/runner container runner",
        "SandboxTemplate/runner container runner",
        "Deployment/with-init container migrate",
        "Deployment/with-init container migrate",
    ]


def test_https_egress_outside_the_cluster_needs_server_names() -> None:
    objects = [
        _policy("proxy", [_https({"toEntities": ["world", "remote-node", "host"]})]),
        _policy("app", [_https({"toEntities": ["remote-node", "host"]}, ["auth.example.test"])]),
        _policy("actions", [_https({"toFQDNs": [{"matchName": "api.example.test"}]})]),
        # In-cluster destinations and other ports are not HTTPS egress to the outside.
        _policy(
            "internal",
            [
                _https({"toEndpoints": [{"matchLabels": {"app": "x"}}]}),
                {"toEntities": ["world"], "toPorts": [{"ports": [{"port": "80"}]}]},
            ],
        ),
    ]
    assert pinned_https_egress(objects, unpinned=frozenset()) == [
        "CiliumNetworkPolicy/proxy: HTTPS egress to ['host', 'remote-node', 'world'] without serverNames",
        "CiliumNetworkPolicy/actions: HTTPS egress to [{'matchName': 'api.example.test'}] without serverNames",
    ]
    assert pinned_https_egress(objects, unpinned=frozenset({"proxy", "actions"})) == []


def _reader(
    name: str, env_secret: str | None = None, volume_secret: str | None = None, config_map: str | None = None
) -> dict[str, Any]:
    container: dict[str, Any] = {"name": "main"}
    pod: dict[str, Any] = {}
    if env_secret:
        container["env"] = [{"name": "X", "valueFrom": {"secretKeyRef": {"name": env_secret, "key": "k"}}}]
    if volume_secret:
        pod["volumes"] = [{"name": "v", "projected": {"sources": [{"secret": {"name": volume_secret}}]}}]
    if config_map:
        pod["volumes"] = [*pod.get("volumes", []), {"name": "c", "configMap": {"name": config_map}}]
    return _deployment(name, containers=[container], pod=pod)


def test_references_resolve_in_chart_and_reject_wrong_kind() -> None:
    objects = [
        {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "metadata": {"name": "es"},
            "spec": {"target": {"name": "es-target"}},
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "ca"},
            "spec": {"secretName": "ca-secret"},
        },
        {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": {"name": "postgres"}, "spec": {}},
        {
            "apiVersion": "trust.cert-manager.io/v1alpha1",
            "kind": "Bundle",
            "metadata": {"name": "ca-bundle"},
            "spec": {},
        },
        _reader("chart-provided", env_secret="es-target", volume_secret="ca-secret", config_map="ca-bundle"),
        _reader("cnpg", env_secret="postgres-app"),
        # External and unmatched references are outside this chart's validation scope.
        _reader("external", env_secret="oidc", config_map="image-tag"),
        _reader("unmatched", env_secret="missing-secret", config_map="missing-map"),
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "wrong-kind"}},
        _reader("wrong-kind", volume_secret="wrong-kind"),
    ]
    assert resolved_references(objects) == [
        "Deployment/wrong-kind reads Secret 'wrong-kind', but the chart creates a ConfigMap with that name"
    ]


def test_rules_fail_synth_naming_the_object() -> None:
    chart = Chart(Cdk8sTesting.app(), "fleet")
    ApiObject(
        chart, "bare", api_version="apps/v1", kind="Deployment", metadata=ApiObjectMetadata(name="bare")
    ).add_json_patch(JsonPatch.add("/spec", {"template": {"spec": {"containers": [{"name": "main"}]}}}))
    add_fleet_rules(chart)
    with pytest.raises(
        Exception, match=r"(?s)Validation failed.*Deployment/bare container main: seccomp profile is None"
    ):
        Cdk8sTesting.synth(chart)


if __name__ == "__main__":
    pytest_bazel.main()
