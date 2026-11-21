"""Static files share the API origin without inheriting its credentials or access."""

from typing import Any

import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import app, frontend, staging
from cluster.cdk8s.testing.manifests import find_resource


def test_static_routes_leave_api_and_auth_on_the_app(staging_manifests: list[dict[str, Any]]) -> None:
    routes = [doc for doc in staging_manifests if doc["kind"] == "HTTPRoute"]
    static = one(doc for doc in routes if doc["metadata"]["name"] == frontend.NAME)
    api = one(doc for doc in routes if doc["metadata"]["name"] == app.NAME)
    assert static["spec"]["hostnames"] == api["spec"]["hostnames"]
    rule = one(static["spec"]["rules"])
    assert one(rule["backendRefs"])["name"] == frontend.service(staging.ENV.namespace).name
    assert {m["path"]["value"] for m in rule["matches"] if m["path"]["type"] == "Exact"} == {
        "/",
        "/index.html",
        "/main.js",
        "/main.css",
        "/sw.js",
        "/favicon.svg",
    }
    assert all(m["path"]["type"] == "Exact" for m in rule["matches"])
    # API URLs and auth keep their current public paths, including long-lived streams.
    api_rule = one(api["spec"]["rules"])
    assert not api_rule.get("matches")
    assert one(api_rule["backendRefs"])["name"] == app.service(staging.ENV.namespace).name
    assert api_rule["timeouts"]["request"] == "3600s"


def test_frontend_has_no_runtime_credentials_or_outbound_access(staging_manifests: list[dict[str, Any]]) -> None:
    pod = find_resource(staging_manifests, "Deployment", frontend.NAME)["spec"]["template"]["spec"]
    account = find_resource(staging_manifests, "ServiceAccount", frontend.NAME)
    assert pod["automountServiceAccountToken"] is False
    assert account["automountServiceAccountToken"] is False
    assert not pod.get("initContainers")
    container = one(pod["containers"])
    assert not container.get("env")
    assert not container.get("envFrom")
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert one(container["volumeMounts"])["mountPath"] == "/tmp"
    assert "emptyDir" in one(pod["volumes"])
    assert not any(
        subject["kind"] == "ServiceAccount" and subject["name"] == frontend.NAME
        for doc in staging_manifests
        if doc["kind"] in {"RoleBinding", "ClusterRoleBinding"}
        for subject in doc.get("subjects", [])
    )
    policy = find_resource(staging_manifests, "CiliumNetworkPolicy", frontend.NAME)["spec"]
    assert policy["egressDeny"] == [{"toEntities": ["all"]}]
    assert not policy.get("egress")
    assert one(policy["ingress"])["fromEntities"] == ["ingress"]


def test_staging_services_keep_replicas_on_separate_hosts(staging_manifests: list[dict[str, Any]]) -> None:
    for name in staging.ENV.hostname_spread_services:
        deployment = find_resource(staging_manifests, "Deployment", name)
        spec = deployment["spec"]
        assert spec["replicas"] >= 2
        spread = one(spec["template"]["spec"]["topologySpreadConstraints"])
        assert spread["labelSelector"]["matchLabels"] == {"app.kubernetes.io/name": name}
        assert spread["topologyKey"] == "kubernetes.io/hostname"
        assert spread["minDomains"] == 2
        assert spread["maxSkew"] == 1
        assert spread["matchLabelKeys"] == ["pod-template-hash"]
        assert spread["nodeTaintsPolicy"] == "Honor"
        assert spread["whenUnsatisfiable"] == "DoNotSchedule"
        assert find_resource(staging_manifests, "PodDisruptionBudget", name)["spec"]["minAvailable"] >= 1


if __name__ == "__main__":
    pytest_bazel.main()
