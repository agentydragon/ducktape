"""The SSH MCP backend and both consumers share a generated URL and bearer contract.

The backend and sshpiper charts are synthesized from their cdk8s constructs here; this
checks their resource relationships without reading committed generated YAML.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import ssh_mcp_config, ssh_mcp_constructs, sshpiper_constructs
from cluster.cdk8s.haku import console_config
from cluster.scripts import nebula_mesh
from util.bazel.runfiles import get_required_path

# pytest_plugins loads cluster.validation.agentplane_fixtures by name; gazelle cannot see
# the dependency.
# gazelle:include_dep //cluster/validation:agentplane_fixtures
pytest_plugins = ("cluster.validation.agentplane_fixtures",)


def _locate(relative: str) -> Path:
    return get_required_path(f"_main/{relative}")


@pytest.fixture(scope="module")
def ssh_config() -> ssh_mcp_config.SshMcpConfig:
    return ssh_mcp_config.load(_locate)


@pytest.fixture(scope="module")
def ssh_resources(ssh_config: ssh_mcp_config.SshMcpConfig) -> list[dict[str, Any]]:
    chart = Cdk8sTesting.chart()
    ssh_mcp_constructs.SshMcp(
        chart, ssh_mcp_config.NAME, config=ssh_config, mesh=nebula_mesh.load(_locate("nebula-mesh.json"))
    )
    return Cdk8sTesting.synth(chart)


@pytest.fixture(scope="module")
def sshpiper_resources(ssh_config: ssh_mcp_config.SshMcpConfig) -> list[dict[str, Any]]:
    chart = Cdk8sTesting.chart()
    sshpiper_constructs.construct(
        chart, config=ssh_config, downstream_key=_locate(ssh_mcp_config.AGENT_DOWNSTREAM_KEY).read_text()
    )
    return Cdk8sTesting.synth(chart)


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
    assert config["url"] == ssh_mcp_config.MCP_URL
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


def test_haku_console_uses_the_same_backend_endpoint() -> None:
    ssh = console_config.config()["mcp"]["servers"]["ssh"]
    endpoint = urlsplit(ssh["backend"]["url"])
    assert ssh["backend"]["auth"]["kind"] == "static_bearer"
    assert endpoint.geturl() == ssh_mcp_config.MCP_URL


def test_bearer_reaches_exactly_the_namespaces_that_may_call(ssh_resources: list[dict[str, Any]]) -> None:
    """The Secret is reflected into the same namespaces the CiliumNetworkPolicy admits."""
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


def test_backend_and_sshpiper_pin_the_canonical_devbox_key(
    ssh_config: ssh_mcp_config.SshMcpConfig,
    ssh_resources: list[dict[str, Any]],
    sshpiper_resources: list[dict[str, Any]],
) -> None:
    canonical_key = _locate(ssh_mcp_config.DEVBOX_HOST_KEY).read_text().split()
    expected = f"{ssh_config.devbox_host} {canonical_key[0]} {canonical_key[1]}"
    config_map = one(r for r in ssh_resources if r["kind"] == "ConfigMap")
    assert expected in config_map["data"]["known_hosts"].splitlines()

    pipe = one(r for r in sshpiper_resources if r["kind"] == "Pipe")
    pipe_known_hosts = base64.b64decode(pipe["spec"]["to"]["known_hosts_data"]).decode().splitlines()
    assert expected in pipe_known_hosts
    assert (
        f"[{ssh_config.devbox_host}]:{ssh_config.devbox_port} {canonical_key[0]} {canonical_key[1]}" in pipe_known_hosts
    )
    assert pipe["spec"]["to"]["host"] == f"{ssh_config.devbox_host}:{ssh_config.devbox_port}"

    settings = yaml.safe_load(config_map["data"]["settings.yaml"])
    assert {target["host"] for target in settings["targets"] if target["host"] == ssh_config.devbox_host} == {
        ssh_config.devbox_host
    }


if __name__ == "__main__":
    pytest_bazel.main()
