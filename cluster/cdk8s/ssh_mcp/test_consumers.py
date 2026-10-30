"""The SSH MCP backend and its consumer share a generated URL and bearer contract.

The backend chart is synthesized from its cdk8s construct here; this checks its resource
relationships without reading committed generated YAML.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_bazel
import yaml
from cdk8s import (
    App,
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one

from cluster.cdk8s.ssh_mcp import backend as ssh_mcp_backend, config as ssh_mcp_config
from cluster.scripts import nebula_mesh
from util.bazel.runfiles import get_required_path


def _locate(relative: str) -> Path:
    return get_required_path(f"_main/{relative}")


@pytest.fixture(scope="module")
def ssh_config() -> ssh_mcp_config.SshMcpConfig:
    return ssh_mcp_config.load()


@pytest.fixture(scope="module")
def ssh_resources(
    scratch_app_factory: Callable[[], App], ssh_config: ssh_mcp_config.SshMcpConfig
) -> list[dict[str, Any]]:
    chart = Chart(scratch_app_factory(), "test")
    ssh_mcp_backend.SshMcp(
        chart, ssh_mcp_config.NAME, config=ssh_config, mesh=nebula_mesh.load(_locate("nebula-mesh.json"))
    )
    return Cdk8sTesting.synth(chart)


def test_actions_binding_uses_the_bearer_ssh_mcp_mints(
    staging_manifests: list[dict[str, Any]], ssh_resources: list[dict[str, Any]]
) -> None:
    settings = yaml.safe_load(
        one(
            doc
            for doc in staging_manifests
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "actions-settings"
        )["data"]["settings.yaml"]
    )
    config = settings["action_groups"]["ssh"]["executor"]["config"]
    assert config["auth"] == "static_bearer"
    assert config["url"] == ssh_mcp_config.MCP_URL
    bearer_file = Path(config["bearer_file"])
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "actions"
    )
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
    reader = {
        "kind": "ServiceAccount",
        "name": provider["auth"]["serviceAccount"]["name"],
        "namespace": copy["metadata"]["namespace"],
    }
    roles = {
        d["metadata"]["name"]: d
        for d in staging_manifests
        if d["kind"] == "Role" and d["metadata"]["namespace"] == namespace
    }
    granted = [
        rule
        for binding in staging_manifests
        if binding["kind"] == "RoleBinding"
        and binding["metadata"]["namespace"] == namespace
        and any({k: s[k] for k in reader} == reader for s in binding["subjects"])
        for rule in roles[binding["roleRef"]["name"]]["rules"]
    ]
    assert granted == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": [remote_ref["key"]], "verbs": ["get"]}
    ]
    backend = one(r for r in ssh_resources if r["kind"] == "Deployment")
    server = one(backend["spec"]["template"]["spec"]["containers"])
    bearer = one(e for e in server["env"] if e["name"] == "SSH_MCP_BEARER_TOKEN")["valueFrom"]["secretKeyRef"]
    assert bearer == {"name": remote_ref["key"], "key": remote_ref["property"]}
    service = one(r for r in ssh_resources if r["kind"] == "Service")
    endpoint = urlsplit(config["url"])
    assert endpoint.hostname == f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc"
    assert endpoint.port == one(service["spec"]["ports"])["port"]
    assert service["spec"]["selector"].items() <= backend["spec"]["template"]["metadata"]["labels"].items()
    # Private SSH material never crosses the backend namespace boundary.
    key_volume = one(v for v in backend["spec"]["template"]["spec"]["volumes"] if v["name"] == "keys")
    assert all(v.get("secret", {}).get("secretName") != key_volume["secret"]["secretName"] for v in pod["volumes"])


def test_bearer_reaches_exactly_the_namespaces_that_may_call(
    staging_manifests: list[dict[str, Any]], ssh_resources: list[dict[str, Any]]
) -> None:
    """The stores on ssh-mcp's namespace admit the namespaces the CiliumNetworkPolicy admits,
    and no Reflector annotation hands the Secret out anywhere else."""
    password = one(r for r in ssh_resources if r["kind"] == "Password")
    source = one(r for r in ssh_resources if r["kind"] == "ExternalSecret" and "secretStoreRef" not in r["spec"])
    assert one(source["spec"]["dataFrom"])["sourceRef"]["generatorRef"] == {
        "apiVersion": password["apiVersion"],
        "kind": password["kind"],
        "name": password["metadata"]["name"],
    }
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
