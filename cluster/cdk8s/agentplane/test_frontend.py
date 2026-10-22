"""Static files share the API origin without inheriting its credentials or access."""

from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import app, frontend
from cluster.cdk8s.agentplane.conftest import NAMESPACES


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_static_routes_leave_api_and_auth_on_the_app(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    routes = [doc for doc in agentplane_manifests[namespace] if doc["kind"] == "HTTPRoute"]
    static = one(doc for doc in routes if doc["metadata"]["name"] == frontend.NAME)
    api = one(doc for doc in routes if doc["metadata"]["name"] == app.NAME)
    assert static["spec"]["hostnames"] == api["spec"]["hostnames"]
    rule = one(static["spec"]["rules"])
    assert one(rule["backendRefs"])["name"] == frontend.service(namespace).name
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
    assert one(api_rule["backendRefs"])["name"] == app.service(namespace).name
    assert api_rule["timeouts"]["request"] == "3600s"


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_frontend_has_no_runtime_credentials_or_outbound_access(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    documents = agentplane_manifests[namespace]
    pod = one(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == frontend.NAME)[
        "spec"
    ]["template"]["spec"]
    account = one(
        doc for doc in documents if doc["kind"] == "ServiceAccount" and doc["metadata"]["name"] == frontend.NAME
    )
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
        for doc in documents
        if doc["kind"] in {"RoleBinding", "ClusterRoleBinding"}
        for subject in doc.get("subjects", [])
    )
    policy = one(
        doc for doc in documents if doc["kind"] == "CiliumNetworkPolicy" and doc["metadata"]["name"] == frontend.NAME
    )["spec"]
    assert policy["egressDeny"] == [{"toEntities": ["all"]}]
    assert not policy.get("egress")
    assert one(policy["ingress"])["fromEntities"] == ["ingress"]


if __name__ == "__main__":
    pytest_bazel.main()
