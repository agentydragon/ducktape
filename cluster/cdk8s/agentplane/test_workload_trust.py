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
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    CENTRAL_SAMPLER_URI,
    EGRESS_PROXY_URI,
    GATEWAY_RUNTIME_PROXY_URI,
    GATEWAY_SESSIONS_URI,
    HISTORY_SERVICE_URI,
    LLM_INGRESS_URI,
    MCP_EDGE_URI,
    NOTIFICATIONS_URI,
    POLICY_CONTROLLER_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
)
from cluster.cdk8s.agentplane import egress, runner_gateway, staging
from cluster.cdk8s.agentplane.sandbox_pod import SIDECAR_SETTINGS_VOLUME_NAME
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource

Body = dict[str, Any]


def _ca_files(value: object) -> Iterator[str]:
    """Every `ca_file` or `client_ca_file` anywhere in a parsed settings document: a listener's SPIFFE authentication,
    a client's `ClientTLS`, the Gateway's `runner_security`, a sidecar's proxy transport. Each names
    roots that verify an Agentplane SVID; settings for any other peer use other keys."""
    if isinstance(value, dict):
        for field in ("ca_file", "client_ca_file"):
            if field in value:
                yield value[field]
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


def test_staging_grants_no_tokenreviews(staging_manifests: list[Body]) -> None:
    for doc in staging_manifests:
        if doc["kind"] in {"Role", "ClusterRole"}:
            for rule in doc.get("rules", []):
                assert "tokenreviews" not in rule.get("resources", []), doc["metadata"]["name"]
        if doc["kind"] in {"RoleBinding", "ClusterRoleBinding"}:
            assert "token-reviewer" not in doc["roleRef"]["name"], doc["metadata"]["name"]


def test_every_deployment_verifies_agentplane_svids_against_the_workload_ca_alone(
    staging_manifests: list[Body],
) -> None:
    trust = _deployment_trust(staging_manifests)
    # Client-SVID listeners (the egress proxy's forward listener, the services through `workload_tls`),
    # backend clients of service SVIDs (the app), and the Gateway dialing runners' server SVIDs.
    assert {egress.NAME, "actions", "llm-ingress", "app", "policy-controller", runner_gateway.NAME} <= trust.keys()
    assert {name: bundles for name, bundles in trust.items() if bundles != {cluster_ca.WORKLOAD_BUNDLE}} == {}


def _service_container(documents: list[Body], name: str) -> tuple[Body, dict[PurePosixPath, Body]]:
    pod = find_resource(documents, "Deployment", name, namespace=staging.ENV.namespace)["spec"]["template"]["spec"]
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    container = one(pod["containers"])
    return container, {PurePosixPath(m["mountPath"]): volumes[m["name"]] for m in container["volumeMounts"]}


def _assert_mounted_tls(
    documents: list[Body], mounts: dict[PurePosixPath, Body], tls: Body, uri: str, *, ca_field: str = "ca_file"
) -> None:
    secrets = set()
    for field, key in (("cert_file", "tls.crt"), ("key_file", "tls.key")):
        path = PurePosixPath(tls[field])
        directory = max((root for root in mounts if root in path.parents), key=lambda root: len(root.parts))
        volume = mounts[directory]["secret"]
        relative = str(path.relative_to(directory))
        if "items" in volume:
            assert one(item for item in volume["items"] if item["path"] == relative)["key"] == key
        else:
            assert relative == key
        secrets.add(volume["secretName"])
    secret = one(secrets)
    cert = one(
        doc
        for doc in documents
        if doc["kind"] == "Certificate"
        and doc["metadata"]["namespace"] == staging.ENV.namespace
        and doc["spec"]["secretName"] == secret
    )
    assert cert["spec"]["uris"] == [uri]
    ca = PurePosixPath(tls[ca_field])
    assert _bundle(mounts[ca.parent], ca.name) == cluster_ca.WORKLOAD_BUNDLE


def test_mcp_tls_listener_uses_its_mounted_certificate_and_only_admits_app(staging_manifests: list[Body]) -> None:
    container, mounts = _service_container(staging_manifests, "mcp-edge")
    settings = config_map_yaml(staging_manifests, "mcp-edge-settings", "settings.yaml")
    tls = settings["rpc_tls"]
    assert settings["operator_services"] == [APP_URI]
    _assert_mounted_tls(staging_manifests, mounts, tls, MCP_EDGE_URI, ca_field="client_ca_file")
    tls_port = int(tls["listen"].rsplit(":", 1)[1])
    listeners = {"http": settings["http_port"], "grpc-tls": tls_port}
    service = find_resource(staging_manifests, "Service", "mcp-edge")
    assert {p["name"]: p["targetPort"] for p in service["spec"]["ports"]} == listeners
    assert {p["name"]: p["containerPort"] for p in container["ports"]} == listeners

    # Gateway and scrape traffic may reach HTTP. Only the staging App may reach TLS.
    policy = find_resource(staging_manifests, "CiliumNetworkPolicy", "mcp-edge")["spec"]
    app_peer = {
        "matchLabels": {"app.kubernetes.io/name": "app", "k8s:io.kubernetes.pod.namespace": staging.ENV.namespace}
    }
    http_ports = [{"ports": [{"port": str(settings["http_port"]), "protocol": "TCP"}]}]
    ingress = policy["ingress"]
    assert any(rule.get("toPorts") == http_ports for rule in ingress)
    assert [rule for rule in ingress if rule.get("toPorts") != http_ports] == [
        {"fromEndpoints": [app_peer], "toPorts": [{"ports": [{"port": str(tls_port), "protocol": "TCP"}]}]}
    ]


def test_policy_service_exposes_its_tls_listener_to_the_app(staging_manifests: list[Body]) -> None:
    container, mounts = _service_container(staging_manifests, "policy-controller")
    settings = config_map_yaml(staging_manifests, "policy-controller-settings", "config.yaml")
    tls = settings["grpc_tls"]
    assert settings["reader_services"] == settings["writer_services"] == [APP_URI]
    _assert_mounted_tls(staging_manifests, mounts, tls, POLICY_CONTROLLER_URI, ca_field="client_ca_file")

    tls_port = int(tls["listen"].removeprefix(":"))
    metrics = int(settings["metrics_bind_address"].removeprefix(":"))
    health = int(settings["health_probe_bind_address"].removeprefix(":"))
    exposed = {"metrics": metrics, "grpc-tls": tls_port}
    assert {p["name"]: p["containerPort"] for p in container["ports"]} == {**exposed, "health": health}
    service = find_resource(staging_manifests, "Service", "policy-controller")
    assert {p["name"]: p["targetPort"] for p in service["spec"]["ports"]} == exposed
    app = config_map_yaml(staging_manifests, "app-config", "config.yaml")
    assert int(app["policy_service_target"].rsplit(":", 1)[1]) == one(
        p["port"] for p in service["spec"]["ports"] if p["name"] == "grpc-tls"
    )
    policy = find_resource(staging_manifests, "CiliumNetworkPolicy", "policy-controller")["spec"]
    assert {p["port"] for rule in policy["ingress"] for group in rule["toPorts"] for p in group["ports"]} == {
        str(metrics),
        str(tls_port),
    }


def test_notifications_calls_backends_with_its_mounted_service_svid(staging_manifests: list[Body]) -> None:
    _, mounts = _service_container(staging_manifests, "notifications")
    settings = config_map_yaml(staging_manifests, "notifications-settings", "settings.yaml")
    for peer, server_uri in {
        "actions": ACTIONS_URI,
        "history_service": HISTORY_SERVICE_URI,
        "session_dispatcher": SESSION_DISPATCHER_URI,
    }.items():
        assert "token_file" not in settings[peer]
        tls = settings[peer]["tls"]
        assert tls["server_uri"] == server_uri
        assert tls["client_uri"] == NOTIFICATIONS_URI
        _assert_mounted_tls(staging_manifests, mounts, tls, NOTIFICATIONS_URI)


def test_app_service_calls_use_its_mounted_svid(staging_manifests: list[Body]) -> None:
    _, mounts = _service_container(staging_manifests, "app")
    settings = config_map_yaml(staging_manifests, "app-config", "config.yaml")
    for name, server_uri in (
        ("egress_admin", EGRESS_PROXY_URI),
        ("runner_gateway", SANDBOX_CONTROLLER_URI),
        ("session_dispatcher", SESSION_DISPATCHER_URI),
        ("history_service", HISTORY_SERVICE_URI),
        ("notifications", NOTIFICATIONS_URI),
        ("actions", ACTIONS_URI),
        ("connection_service", MCP_EDGE_URI),
        ("policy_service", POLICY_CONTROLLER_URI),
    ):
        tls = settings[f"{name}_tls"]
        assert tls["server_uri"] == server_uri
        assert tls["client_uri"] == APP_URI
        assert f"{name}_token_file" not in settings
        _assert_mounted_tls(staging_manifests, mounts, tls, APP_URI)

    port = int(settings["connection_service_url"].rstrip("/").rsplit(":", 1)[1])
    edge_service = find_resource(staging_manifests, "Service", "mcp-edge")
    assert one(p for p in edge_service["spec"]["ports"] if p["name"] == "grpc-tls")["port"] == port
    policy = find_resource(staging_manifests, "CiliumNetworkPolicy", "app-to-mcp-edge")
    assert any(
        int(p["port"]) == port
        for rule in policy["spec"]["egress"]
        for ports in rule.get("toPorts", [])
        for p in ports["ports"]
    )


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


def test_service_svids_are_mounted_with_client_and_server_usage(staging_manifests: list[Body]) -> None:
    service_uris = {
        ACTIONS_URI,
        APP_URI,
        CENTRAL_SAMPLER_URI,
        EGRESS_PROXY_URI,
        GATEWAY_RUNTIME_PROXY_URI,
        GATEWAY_SESSIONS_URI,
        HISTORY_SERVICE_URI,
        LLM_INGRESS_URI,
        MCP_EDGE_URI,
        NOTIFICATIONS_URI,
        POLICY_CONTROLLER_URI,
        SANDBOX_CONTROLLER_URI,
        SESSION_DISPATCHER_URI,
    }
    certificates = [
        doc
        for doc in staging_manifests
        if doc["kind"] == "Certificate" and service_uris.intersection(doc["spec"].get("uris", []))
    ]
    assert {uri for certificate in certificates for uri in certificate["spec"]["uris"]} == service_uris

    mounted_secrets: set[tuple[str, str]] = set()
    for deployment in (doc for doc in staging_manifests if doc["kind"] == "Deployment"):
        pod = deployment["spec"]["template"]["spec"]
        read_only = {
            mount["name"]
            for container in pod["containers"]
            for mount in container.get("volumeMounts", [])
            if mount.get("readOnly")
        }
        mounted_secrets.update(
            (deployment["metadata"]["namespace"], volume["secret"]["secretName"])
            for volume in pod.get("volumes", [])
            if volume["name"] in read_only and "secret" in volume
        )

    mounted_uris: set[str] = set()
    # Several leaves may carry one service URI while replacement credentials are prepared.
    for certificate in certificates:
        assert {"digital signature", "client auth", "server auth"} <= set(certificate["spec"]["usages"])
        if (certificate["metadata"]["namespace"], certificate["spec"]["secretName"]) in mounted_secrets:
            mounted_uris.update(certificate["spec"]["uris"])
    assert mounted_uris == service_uris, (
        f"Service URIs without a read-only mounted Certificate: {service_uris - mounted_uris}"
    )


def test_the_workload_ca_chains_to_the_cluster_root_and_its_bundle_carries_it_alone(scratch_app: App) -> None:
    documents = cdk8s.Testing.synth(cluster_ca.chart(scratch_app))
    issuer = find_resource(documents, "ClusterIssuer", cluster_ca.WORKLOAD_ISSUER.name)
    ca_secret = issuer["spec"]["ca"]["secretName"]
    ca = one(doc for doc in documents if doc["kind"] == "Certificate" and doc["spec"]["secretName"] == ca_secret)
    # A ClusterIssuer reads its CA from cert-manager's own namespace.
    assert ca["metadata"]["namespace"] == "cert-manager"
    assert ca["spec"]["isCA"] is True
    # Chained to the cluster root, so clients verifying server certificates with the cluster bundle accept its leaves.
    assert ca["spec"]["issuerRef"] == {"name": cluster_ca.INTERNAL_ISSUER.name, "kind": cluster_ca.INTERNAL_ISSUER.kind}

    # The intermediate alone: cert-manager writes a CA's `tls.crt` without the self-signed root.
    workload = find_resource(documents, "Bundle", cluster_ca.WORKLOAD_BUNDLE)["spec"]
    assert workload["sources"] == [{"secret": {"name": ca_secret, "key": "tls.crt"}}]
    # Every namespace, like the cluster bundle: it is public trust data.
    assert "namespaceSelector" not in workload["target"]
    assert workload["target"]["configMap"]["key"] == cluster_ca.BUNDLE_KEY
    # The cluster bundle carries roots only, not the intermediate.
    assert not any(
        source.get("secret", {}).get("name") == ca_secret
        for source in find_resource(documents, "Bundle", cluster_ca.BUNDLE)["spec"]["sources"]
    )


if __name__ == "__main__":
    pytest_bazel.main()
