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

from cluster.cdk8s.fleet_rules import (
    add_fleet_rules,
    https_egress_sni_conflicts,
    pinned_https_egress,
    pod_hardening,
    resolved_references,
)

_HARDENED = {"securityContext": {"seccompProfile": {"type": "RuntimeDefault"}}}
_NO_TOKEN = {"automountServiceAccountToken": False}
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


def _policy_for(name: str, egress: list[dict[str, Any]], *, namespace: str, labels: dict[str, str]) -> dict[str, Any]:
    policy = _policy(name, egress)
    policy["metadata"]["namespace"] = namespace
    policy["spec"]["endpointSelector"] = {"matchLabels": labels}
    return policy


def test_pod_hardening_names_the_container_and_what_it_lacks() -> None:
    objects = [
        _deployment("bare", containers=[{"name": "main", "resources": {"requests": {"cpu": "1"}}}]),
        _deployment("ok", containers=[{"name": "main", "resources": _RESOURCES}], pod={**_HARDENED, **_NO_TOKEN}),
        _deployment("own-profile", containers=[{"name": "main", "resources": _RESOURCES, **_HARDENED}], pod=_NO_TOKEN),
    ]
    assert pod_hardening(objects) == [
        "Deployment/bare: automountServiceAccountToken is unset",
        "Deployment/bare container main: seccomp profile is None, not RuntimeDefault",
        "Deployment/bare container main: resources.requests lacks ['memory']",
        "Deployment/bare container main: resources.limits lacks ['memory']",
    ]


def test_pod_hardening_covers_init_containers_and_crd_pod_templates() -> None:
    sandbox = {
        "apiVersion": "extensions.agents.x-k8s.io/v1alpha1",
        "kind": "SandboxTemplate",
        "metadata": {"name": "runner"},
        "spec": {"podTemplate": {"spec": {**_HARDENED, **_NO_TOKEN, "containers": [{"name": "runner"}]}}},
    }
    scaled_job = {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledJob",
        "metadata": {"name": "ci"},
        "spec": {
            "jobTargetRef": {
                "template": {"spec": {**_NO_TOKEN, "containers": [{"name": "job", "resources": _RESOURCES}]}}
            }
        },
    }
    deployment = _deployment(
        "with-init",
        containers=[{"name": "main", "resources": _RESOURCES}],
        pod={**_HARDENED, **_NO_TOKEN, "initContainers": [{"name": "migrate"}]},
    )
    assert [error.split(":")[0] for error in pod_hardening([sandbox, scaled_job, deployment])] == [
        "SandboxTemplate/runner container runner",
        "SandboxTemplate/runner container runner",
        "ScaledJob/ci container job",
        "Deployment/with-init container migrate",
        "Deployment/with-init container migrate",
    ]


def test_pod_hardening_mounts_a_token_only_where_stated_under_an_own_account() -> None:
    containers = [{"name": "main", "resources": _RESOURCES}]
    objects = [
        _deployment("unstated", containers=containers, pod=_HARDENED),
        _deployment("implicit-default", containers=containers, pod={**_HARDENED, "automountServiceAccountToken": True}),
        _deployment(
            "named-default",
            containers=containers,
            pod={**_HARDENED, "automountServiceAccountToken": True, "serviceAccountName": "default"},
        ),
        _deployment(
            "own-account",
            containers=containers,
            pod={**_HARDENED, "automountServiceAccountToken": True, "serviceAccountName": "resigner"},
        ),
        _deployment("no-token", containers=containers, pod={**_HARDENED, **_NO_TOKEN}),
    ]
    assert pod_hardening(objects) == [
        "Deployment/unstated: automountServiceAccountToken is unset",
        "Deployment/implicit-default: automountServiceAccountToken is true under the default ServiceAccount",
        "Deployment/named-default: automountServiceAccountToken is true under the default ServiceAccount",
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


_STAGING = "agentplane-staging"


def test_an_sni_pin_may_not_share_an_endpoint_with_open_https_egress() -> None:
    """The pair that broke every `*.allegedly.works` name a proxy served: one policy pinning SNI on
    node:443, a sibling leaving that port open for the same Pods. Cilium enforces the pin per port
    on the endpoint across both policies, so the open rule silently stops being open."""
    proxy = {"app.kubernetes.io/name": "agentplane-egress"}
    open_egress = _policy_for(
        "egress", [_https({"toEntities": ["world", "remote-node", "host"]})], namespace=_STAGING, labels=proxy
    )
    pinned_egress = _policy_for(
        "egress-to-testing-app",
        [_https({"toEntities": ["remote-node", "host"]}, ["agentplane-testing.allegedly.works"])],
        namespace=_STAGING,
        labels=proxy,
    )
    conflict = (
        "CiliumNetworkPolicy/egress-to-testing-app pins SNI on outside HTTPS while "
        "CiliumNetworkPolicy/egress leaves port 443 open for the same endpoints: the pin decides "
        "every name on that port, not just its own"
    )
    assert https_egress_sni_conflicts([open_egress, pinned_egress]) == [conflict]
    # Order decides nothing, and neither half conflicts on its own.
    assert https_egress_sni_conflicts([pinned_egress, open_egress]) == [conflict]
    assert https_egress_sni_conflicts([open_egress]) == []
    assert https_egress_sni_conflicts([pinned_egress]) == []
    # A selector naming a different Pod, and a namespaced policy in a namespace the other cannot
    # reach, both stay legal.
    elsewhere = _policy_for("elsewhere", pinned_egress["spec"]["egress"], namespace="elsewhere", labels=proxy)
    another = {"app.kubernetes.io/name": "agentplane-app"}
    other_pod = _policy_for("other-pod", pinned_egress["spec"]["egress"], namespace=_STAGING, labels=another)
    assert https_egress_sni_conflicts([open_egress, elsewhere]) == []
    assert https_egress_sni_conflicts([open_egress, other_pod]) == []
    # Everything else overlaps, because `matchLabels` matches endpoints carrying *at least* those
    # labels: a narrower selector (subset) and a disjoint key set both describe endpoints that
    # satisfy the open rule too.
    subset = _policy_for("subset", pinned_egress["spec"]["egress"], namespace=_STAGING, labels=proxy | {"tier": "x"})
    disjoint_keys = _policy_for("odd", pinned_egress["spec"]["egress"], namespace=_STAGING, labels={"tier": "x"})
    assert len(https_egress_sni_conflicts([open_egress, subset])) == 1
    assert len(https_egress_sni_conflicts([subset, open_egress])) == 1
    assert len(https_egress_sni_conflicts([open_egress, disjoint_keys])) == 1
    # An SNI rule on a port nothing else uses is not a conflict: the L7 layer is per port. That is
    # how the proxy keeps Dex's SNI scoping and its open 443 side by side.
    dex = _policy_for(
        "dex",
        [{"toPorts": [{"ports": [{"port": "5556", "protocol": "TCP"}], "serverNames": ["dex.test"]}]}],
        namespace=_STAGING,
        labels=proxy,
    )
    assert https_egress_sni_conflicts([open_egress, dex]) == []
    # 443 on in-cluster peers is not outside HTTPS: neither an endpoint with SNI nor `cluster`.
    internal = _policy_for(
        "internal",
        [
            _https({"toEndpoints": [{"matchLabels": {"app": "x"}}]}, ["internal.test"]),
            {"toEntities": ["cluster"], "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]},
        ],
        namespace=_STAGING,
        labels=proxy,
    )
    assert https_egress_sni_conflicts([open_egress, internal]) == []
    # A selector this cannot reduce to labels -- none at all, or `matchExpressions` -- covers
    # everything in scope, so it conflicts rather than being skipped.
    no_selector = _policy("catch-all", [_https({"toEntities": ["remote-node", "host"]}, ["a.test"])])
    assert len(https_egress_sni_conflicts([open_egress, no_selector])) == 1
    expressions = _policy_for("expressions", pinned_egress["spec"]["egress"], namespace=_STAGING, labels={})
    expressions["spec"]["endpointSelector"]["matchExpressions"] = [
        {"key": "app.kubernetes.io/name", "operator": "In", "values": ["agentplane-egress"]}
    ]
    assert len(https_egress_sni_conflicts([open_egress, expressions])) == 1
    # The same shape inside one policy -- a pinned rule and an open one on 443 -- is the same
    # contradiction, and is named as its own error.
    both = _policy_for(
        "both",
        [_https({"toEntities": ["world", "remote-node", "host"]}), _https({"toEntities": ["remote-node"]}, ["a.test"])],
        namespace=_STAGING,
        labels=proxy,
    )
    assert https_egress_sni_conflicts([both]) == [
        "CiliumNetworkPolicy/both pins SNI on outside HTTPS and leaves port 443 open beside it: the pin "
        "decides every name on that port, not just the ones it lists"
    ]
    # A clusterwide policy reaches every namespace, so it conflicts across the namespace boundary.
    clusterwide = {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumClusterwideNetworkPolicy",
        "metadata": {"name": "wide"},
        "spec": {
            "egress": [_https({"toEntities": ["remote-node", "host"]}, ["a.test"])],
            "endpointSelector": {"matchLabels": proxy},
        },
    }
    assert len(https_egress_sni_conflicts([open_egress, clusterwide])) == 1


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
