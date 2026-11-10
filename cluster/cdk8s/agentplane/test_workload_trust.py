"""Staging's SVIDs come from the workload CA, which chains to the cluster root, and every SPIFFE
verifier, of a client or a server SVID, trusts that CA alone."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import PurePosixPath
from typing import Any

import cdk8s
import pytest_bazel
import yaml
from cdk8s import App
from more_itertools import one

from agentplane.egress import sidecar_config
from cluster.cdk8s.agentplane import egress, runner_gateway, staging
from cluster.cdk8s.agentplane.sandbox_pod import SIDECAR_SETTINGS_VOLUME_NAME
from cluster.cdk8s.cert_manager import cluster_ca

Body = dict[str, Any]


def _resource(documents: list[Body], kind: str, name: str) -> Body:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _ca_files(value: object) -> Iterator[str]:
    """Every `ca_file` anywhere in a parsed settings document: a listener's SPIFFE authentication,
    a client's `ClientTLS`, the Gateway's `runner_security`, a sidecar's proxy transport. Each names
    roots that verify an Agentplane SVID; settings for any other peer use other keys."""
    if isinstance(value, dict):
        if "ca_file" in value:
            yield value["ca_file"]
        for child in value.values():
            yield from _ca_files(child)
    elif isinstance(value, list):
        for child in value:
            yield from _ca_files(child)


def _probe_ca_files(container: Body) -> Iterator[str]:
    """The roots each `healthprobe` verifies its own service's server SVID with."""
    for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
        command = container.get(probe, {}).get("exec", {}).get("command", [])
        if "--ca-file" in command:
            yield command[command.index("--ca-file") + 1]


def _bundle(volume: Body, file: str) -> str:
    """The ConfigMap a mounted volume serves `file` from."""
    if "configMap" in volume:
        return str(volume["configMap"]["name"])
    return str(
        one(
            source["configMap"]["name"]
            for source in volume["projected"]["sources"]
            if "configMap" in source and any(item["path"] == file for item in source["configMap"].get("items", []))
        )
    )


def _container_trust(container: Body, volumes: Mapping[str, Body], config_maps: Mapping[str, Body]) -> set[str]:
    """The ConfigMaps a container's SPIFFE `ca_file`s, in its mounted settings and its probes, resolve to."""
    mounts = {PurePosixPath(m["mountPath"]): volumes[m["name"]] for m in container.get("volumeMounts", [])}
    ca_files = set(_probe_ca_files(container))
    for volume in mounts.values():
        settings = config_maps.get(volume.get("configMap", {}).get("name"))
        for key, content in (settings or {}).get("data", {}).items():
            if key.endswith((".yaml", ".yml", ".json")):
                ca_files.update(_ca_files(yaml.safe_load(content)))
    return {_bundle(mounts[PurePosixPath(path).parent], PurePosixPath(path).name) for path in ca_files}


def _config_maps(documents: list[Body], namespace: str) -> dict[str, Body]:
    return {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc["kind"] == "ConfigMap" and doc["metadata"].get("namespace") == namespace
    }


def _deployment_trust(documents: list[Body]) -> dict[str, set[str]]:
    """Per Deployment, the ConfigMaps its SPIFFE verifiers' `ca_file`s resolve to through its mounts."""
    trust: dict[str, set[str]] = {}
    for deployment in (doc for doc in documents if doc["kind"] == "Deployment"):
        config_maps = _config_maps(documents, deployment["metadata"]["namespace"])
        pod = deployment["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume for volume in pod.get("volumes", [])}
        for container in pod["containers"]:
            if bundles := _container_trust(container, volumes, config_maps):
                trust.setdefault(deployment["metadata"]["name"], set()).update(bundles)
    return trust


def test_every_deployment_verifies_agentplane_svids_against_the_workload_ca_alone(
    staging_manifests: list[Body],
) -> None:
    trust = _deployment_trust(staging_manifests)
    # Client-SVID listeners (the egress proxy's forward listener, the services through `workload_tls`),
    # backend clients of service SVIDs (the app), and the Gateway dialing runners' server SVIDs.
    assert {egress.NAME, "actions", "llm-ingress", "app", runner_gateway.NAME} <= trust.keys()
    assert {name: bundles for name, bundles in trust.items() if bundles != {cluster_ca.WORKLOAD_BUNDLE}} == {}


def _template_pods(documents: list[Body]) -> Iterator[tuple[str, Body]]:
    """Every Sandbox Pod shape: Pod and Firecracker SandboxTemplates, and the command boxes' PodTemplates."""
    for doc in documents:
        if doc["kind"] == "SandboxTemplate":
            spec = doc["spec"]
            pod = spec.get("pod") or spec.get("firecracker", {}).get("pod")
            if pod is not None:
                yield doc["metadata"]["name"], pod["spec"]
        elif doc["kind"] == "PodTemplate":
            yield doc["metadata"]["name"], doc["template"]["spec"]


def test_every_egress_sidecar_verifies_server_svids_against_the_workload_ca_alone(
    staging_manifests: list[Body],
) -> None:
    config_maps = _config_maps(staging_manifests, staging.ENV.namespace)
    sidecars: dict[str, str] = {}
    for name, pod in _template_pods(staging_manifests):
        sidecar = next(
            (
                c
                for c in [*pod.get("initContainers", []), *pod["containers"]]
                if c["name"] == sidecar_config.CONTAINER_NAME
            ),
            None,
        )
        if sidecar is None:
            continue
        settings_volume = one(volume for volume in pod["volumes"] if volume["name"] == SIDECAR_SETTINGS_VOLUME_NAME)
        settings = yaml.safe_load(one(config_maps[settings_volume["configMap"]["name"]]["data"].values()))
        ca_file = settings["proxy"]["transport"]["ca_file"]
        if PurePosixPath(ca_file).parent == PurePosixPath(sidecar_config.RUNNER_TLS_MOUNT_PATH):
            # The Sandbox Controller projects this directory; its workload-CA file is the only one it
            # fills from the workload bundle.
            sidecars[name] = ca_file
        else:
            volumes = {volume["name"]: volume for volume in pod["volumes"]}
            mounts = {PurePosixPath(m["mountPath"]): volumes[m["name"]] for m in sidecar.get("volumeMounts", [])}
            sidecars[name] = _bundle(mounts[PurePosixPath(ca_file).parent], PurePosixPath(ca_file).name)
    # The Pod and Firecracker runners, through the controller's projection, and the command boxes.
    assert {"environment-tools", "sandbox"} <= sidecars.keys()
    assert {
        name: trust
        for name, trust in sidecars.items()
        if trust not in {str(sidecar_config.RUNNER_WORKLOAD_TRUST_FILE), cluster_ca.WORKLOAD_BUNDLE}
    } == {}


def test_agentplane_svids_come_from_the_workload_ca(staging_manifests: list[Body]) -> None:
    svids = [
        doc
        for doc in staging_manifests
        if doc["kind"] == "Certificate"
        and any(uri.startswith("spiffe://agentplane/") for uri in doc["spec"].get("uris", []))
    ]
    assert svids
    assert {
        doc["metadata"]["name"]
        for doc in svids
        if doc["spec"]["issuerRef"]
        != {"name": cluster_ca.WORKLOAD_ISSUER.name, "kind": cluster_ca.WORKLOAD_ISSUER.kind}
    } == set()


def test_the_workload_ca_chains_to_the_cluster_root_and_its_bundle_carries_it_alone(scratch_app: App) -> None:
    documents = cdk8s.Testing.synth(cluster_ca.chart(scratch_app))
    issuer = _resource(documents, "ClusterIssuer", cluster_ca.WORKLOAD_ISSUER.name)
    ca_secret = issuer["spec"]["ca"]["secretName"]
    ca = one(doc for doc in documents if doc["kind"] == "Certificate" and doc["spec"]["secretName"] == ca_secret)
    # A ClusterIssuer reads its CA from cert-manager's own namespace.
    assert ca["metadata"]["namespace"] == "cert-manager"
    assert ca["spec"]["isCA"] is True
    # Chained to the cluster root, so clients verifying server certificates with the cluster bundle accept its leaves.
    assert ca["spec"]["issuerRef"] == {"name": cluster_ca.INTERNAL_ISSUER.name, "kind": cluster_ca.INTERNAL_ISSUER.kind}

    # The intermediate alone: cert-manager writes a CA's `tls.crt` without the self-signed root.
    workload = _resource(documents, "Bundle", cluster_ca.WORKLOAD_BUNDLE)["spec"]
    assert workload["sources"] == [{"secret": {"name": ca_secret, "key": "tls.crt"}}]
    # Every namespace, like the cluster bundle: it is public trust data.
    assert "namespaceSelector" not in workload["target"]
    assert workload["target"]["configMap"]["key"] == cluster_ca.BUNDLE_KEY
    # The cluster bundle carries roots only, not the intermediate.
    assert not any(
        source.get("secret", {}).get("name") == ca_secret
        for source in _resource(documents, "Bundle", cluster_ca.BUNDLE)["spec"]["sources"]
    )


if __name__ == "__main__":
    pytest_bazel.main()
