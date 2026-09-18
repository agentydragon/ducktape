"""agentplane-staging's Actions service reaches ssh-mcp with the bearer ssh-mcp mints.

ssh-mcp is hand-written YAML and the Actions binding is generated, so nothing computes one
side from the other yet (cluster/cdk8s/TODO.md); until ssh-mcp converts, this relates the two.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path

# pytest_plugins loads cluster.validation.agentplane_fixtures by name; gazelle cannot see
# the dependency.
# gazelle:include_dep //cluster/validation:agentplane_fixtures
pytest_plugins = ("cluster.validation.agentplane_fixtures",)


@pytest.fixture(scope="module")
def ssh_resources() -> list[dict[str, Any]]:
    kustomize = get_required_path("multitool/tools/kustomize/kustomize")
    root = get_required_path("_main/cluster/k8s/ssh-mcp/kustomization.yaml").parent
    return list(yaml.safe_load_all(subprocess.check_output([str(kustomize), "build", str(root)])))


def test_actions_binding_uses_the_bearer_ssh_mcp_mints(
    agentplane_manifests: dict[str, list[dict[str, Any]]], ssh_resources: list[dict[str, Any]]
) -> None:
    documents = agentplane_manifests["agentplane-staging"]
    settings = yaml.safe_load(
        one(
            doc
            for doc in documents
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-actions-settings"
        )["data"]["settings.yaml"]
    )
    config = settings["action_groups"]["ssh"]["executor"]["config"]
    assert config["auth"] == "static_bearer"
    bearer_file = Path(config["bearer_file"])
    deployment = one(
        doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "agentplane-actions"
    )
    pod = deployment["spec"]["template"]["spec"]
    actions = one(pod["containers"])
    mount = one(m for m in actions["volumeMounts"] if Path(m["mountPath"]) == bearer_file.parent)
    assert mount["readOnly"]
    volume = one(v for v in pod["volumes"] if v["name"] == mount["name"])
    key = one(i for i in volume["secret"]["items"] if i["path"] == bearer_file.name)["key"]
    source = one(
        r
        for r in ssh_resources
        if r["kind"] == "ExternalSecret" and "dataFrom" in r["spec"] and "secretStoreRef" not in r["spec"]
    )
    assert volume["secret"]["secretName"] == source["spec"]["target"]["name"]
    assert key in source["spec"]["target"]["template"]["data"]
    assert volume["secret"]["secretName"] in deployment["metadata"]["annotations"][
        "secret.reloader.stakater.com/reload"
    ].split(",")
    backend = one(r for r in ssh_resources if r["kind"] == "Deployment")
    server = one(backend["spec"]["template"]["spec"]["containers"])
    bearer = one(e for e in server["env"] if e["name"] == "SSH_MCP_BEARER_TOKEN")["valueFrom"]["secretKeyRef"]
    assert bearer == {"name": volume["secret"]["secretName"], "key": key}
    service = one(r for r in ssh_resources if r["kind"] == "Service")
    endpoint = urlsplit(config["url"])
    assert endpoint.hostname == f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc.cluster.local"
    assert endpoint.port == one(service["spec"]["ports"])["port"]
    assert service["spec"]["selector"].items() <= backend["spec"]["template"]["metadata"]["labels"].items()
    # Private SSH material never crosses the backend namespace boundary.
    key_volume = one(v for v in backend["spec"]["template"]["spec"]["volumes"] if v["name"] == "keys")
    assert all(v.get("secret", {}).get("secretName") != key_volume["secret"]["secretName"] for v in pod["volumes"])


def test_bearer_reaches_exactly_the_namespaces_that_may_call(ssh_resources: list[dict[str, Any]]) -> None:
    """The Secret is reflected into the same namespaces the CiliumNetworkPolicy admits, and the
    Actions service's namespace is one of them."""
    password = one(r for r in ssh_resources if r["kind"] == "Password")
    source = one(r for r in ssh_resources if r["kind"] == "ExternalSecret" and "secretStoreRef" not in r["spec"])
    assert one(source["spec"]["dataFrom"])["sourceRef"]["generatorRef"] == {
        "apiVersion": password["apiVersion"],
        "kind": password["kind"],
        "name": password["metadata"]["name"],
    }
    annotations = source["spec"]["target"]["template"]["metadata"]["annotations"]
    reflected = {
        mode: {
            ns.strip("^$")
            for ns in annotations[f"reflector.v1.k8s.emberstack.com/reflection-{mode}-namespaces"].split(",")
        }
        for mode in ("allowed", "auto")
    }
    policy = one(r for r in ssh_resources if r["kind"] == "CiliumNetworkPolicy")
    admitted = {
        rule["matchLabels"]["k8s:io.kubernetes.pod.namespace"]
        for rule in one(policy["spec"]["ingress"])["fromEndpoints"]
    }
    assert reflected["allowed"] == reflected["auto"] == admitted
    assert "agentplane-staging" in admitted


if __name__ == "__main__":
    pytest_bazel.main()
