"""SSH MCP credential delivery, access and network admission in staging."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
from cdk8s import (
    App,
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one

from agentplane.action_service.catalog import McpExecutorBinding
from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac
from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.ssh_mcp import backend as ssh_mcp_backend, config as ssh_mcp_config
from cluster.cdk8s.testing.manifests import find_resource
from cluster.scripts import nebula_mesh


@pytest.fixture(scope="module")
def ssh_resources(scratch_app_factory: Callable[[], App], nebula_roster: nebula_mesh.Mesh) -> list[dict[str, Any]]:
    chart = Chart(scratch_app_factory(), "test")
    ssh_mcp_backend.SshMcp(chart, ssh_mcp_config.NAME, config=ssh_mcp_config.load(), mesh=nebula_roster)
    return Cdk8sTesting.synth(chart)


def test_actions_binding_uses_the_bearer_ssh_mcp_mints(
    staging_manifests: list[dict[str, Any]], ssh_resources: list[dict[str, Any]]
) -> None:
    executor = staging.ENV.actions.action_groups["ssh"].executor
    assert isinstance(executor, McpExecutorBinding)
    assert executor.config["auth"] == "static_bearer"
    assert executor.config["url"] == ssh_mcp_config.MCP_URL
    path = executor.config["bearer_file"]
    assert isinstance(path, str)
    bearer_file = Path(path)
    deployment = find_resource(staging_manifests, "Deployment", "actions")
    pod = deployment["spec"]["template"]["spec"]
    actions = one(pod["containers"])
    mount = one(m for m in actions["volumeMounts"] if Path(m["mountPath"]) == bearer_file.parent)
    assert mount["readOnly"]
    volume = one(v for v in pod["volumes"] if v["name"] == mount["name"])
    key = one(i for i in volume["secret"]["items"] if i["path"] == bearer_file.name)["key"]
    assert volume["secret"]["secretName"] in deployment["metadata"]["annotations"][
        "secret.reloader.stakater.com/reload"
    ].split(",")
    # The mounted Secret is ESO's copy of the one ssh-mcp mints.
    copy = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "ExternalSecret" and doc["spec"]["target"]["name"] == volume["secret"]["secretName"]
    )
    remote_ref = one(d for d in copy["spec"]["data"] if d["secretKey"] == key)["remoteRef"]
    source = one(
        r
        for r in ssh_resources
        if r["kind"] == "ExternalSecret" and "dataFrom" in r["spec"] and "secretStoreRef" not in r["spec"]
    )
    assert remote_ref["key"] == source["spec"]["target"]["name"]
    assert remote_ref["property"] in source["spec"]["target"]["template"]["data"]
    store_ref = copy["spec"]["secretStoreRef"]
    store = one(
        d for d in staging_manifests if (d["kind"], d["metadata"]["name"]) == (store_ref["kind"], store_ref["name"])
    )
    provider = store["spec"]["provider"]["kubernetes"]
    namespace = source["metadata"]["namespace"]
    assert provider["remoteNamespace"] == namespace
    assert copy["metadata"]["namespace"] in {ns for c in store["spec"]["conditions"] for ns in c["namespaces"]}
    # ssh-mcp's namespace also holds every target's private key: the store's reader may get
    # the bearer and nothing else there.
    reader = provider["auth"]["serviceAccount"]
    grants = Rbac(staging_manifests).identity(
        "ServiceAccount", reader["name"], reader.get("namespace", copy["metadata"]["namespace"])
    )
    assert {
        grant
        for grant in grants
        if grant.namespace in (None, namespace) and grant.api_group in ("", "*") and grant.resource in ("secrets", "*")
    } == {Permission(namespace, "", "secrets", "get", remote_ref["key"])}
    backend = one(r for r in ssh_resources if r["kind"] == "Deployment")
    server = one(backend["spec"]["template"]["spec"]["containers"])
    bearer = one(e for e in server["env"] if e["name"] == "SSH_MCP_BEARER_TOKEN")["valueFrom"]["secretKeyRef"]
    assert bearer == {"name": remote_ref["key"], "key": remote_ref["property"]}


def test_bearer_reaches_exactly_the_namespaces_that_may_call(
    staging_manifests: list[dict[str, Any]], ssh_resources: list[dict[str, Any]]
) -> None:
    """The stores on ssh-mcp's namespace admit the namespaces the CiliumNetworkPolicy admits,
    and no Reflector annotation hands the Secret out anywhere else."""
    source = one(r for r in ssh_resources if r["kind"] == "ExternalSecret" and "secretStoreRef" not in r["spec"])
    annotations = source["spec"]["target"]["template"].get("metadata", {}).get("annotations", {})
    assert not any(annotation.startswith("reflector.v1.k8s.emberstack.com/") for annotation in annotations)
    readers = {
        namespace
        for store in staging_manifests
        if store["kind"] == "ClusterSecretStore"
        and store["spec"]["provider"].get("kubernetes", {}).get("remoteNamespace") == source["metadata"]["namespace"]
        for condition in store["spec"]["conditions"]
        for namespace in condition["namespaces"]
    }
    policy = one(r for r in ssh_resources if r["kind"] == "CiliumNetworkPolicy")
    admitted = {
        rule["matchLabels"]["k8s:io.kubernetes.pod.namespace"]
        for rule in one(policy["spec"]["ingress"])["fromEndpoints"]
    }
    assert readers == admitted
    assert "agentplane-staging" in admitted


if __name__ == "__main__":
    pytest_bazel.main()
