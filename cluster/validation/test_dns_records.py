"""Validates public DNS records and their ingress consumers against the cluster
node roster."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from cluster.validation.terraform_hcl import locals_blocks
from util.bazel.runfiles import get_required_path


def _terraform_local_ips(path: Path, name: str) -> set[str]:
    # pygohcl decodes the `local.<name>` IP list straight to a Python list of strings
    # — a structural parse, not a regex over the file text.
    for block in locals_blocks(path):
        if name in block:
            return set(block[name])
    raise AssertionError(f"{path}: missing local.{name} list")


def _mesh_hosts(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    hosts = data.get("hosts")
    assert isinstance(hosts, dict), f"{path}: expected top-level hosts object"
    return hosts


def _endpoint_ip(host: dict[str, Any]) -> str | None:
    endpoint = host.get("endpoint")
    if not isinstance(endpoint, str):
        return None
    ip, separator, port = endpoint.rpartition(":")
    assert separator, f"endpoint {endpoint!r} must be host:port"
    assert port == "4242", f"endpoint {endpoint!r} should use the Nebula public port"
    return ip


def _public_kubernetes_node_ips(hosts: dict[str, Any]) -> set[str]:
    return {
        ip
        for host in hosts.values()
        if host.get("role") in {"control-plane", "worker"}
        for ip in [_endpoint_ip(host)]
        if ip is not None
    }


def _public_control_plane_ips(hosts: dict[str, Any]) -> set[str]:
    return {
        ip
        for host in hosts.values()
        if host.get("role") == "control-plane"
        for ip in [_endpoint_ip(host)]
        if ip is not None
    }


def test_wildcard_dns_records_match_public_kubernetes_nodes() -> None:
    """allegedly.works and *.allegedly.works should resolve to all public k8s nodes."""
    hosts = _mesh_hosts(get_required_path("_main/nebula-mesh.json"))
    dns_records_tf = get_required_path("_main/tf/gitops/dns-records/main.tf")

    expected = _public_kubernetes_node_ips(hosts)
    assert expected, "nebula-mesh.json should contain at least one public Kubernetes node"
    assert _terraform_local_ips(dns_records_tf, "public_gateway_ips") == expected


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _resource(path: Path, kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in _documents(path) if doc["kind"] == kind and doc["metadata"]["name"] == name)


def test_mailbox_smtp_ingress_covers_public_kubernetes_nodes() -> None:
    """Every public MX node should run a source-preserving port-25 proxy."""
    hosts = _mesh_hosts(get_required_path("_main/nebula-mesh.json"))
    service_yaml = get_required_path("_main/cluster/k8s/haku/mailbox/app/service.yaml")
    ingress_yaml = get_required_path("_main/cluster/k8s/haku/mailbox/app/smtp-ingress.yaml")
    cilium_values = yaml.safe_load(get_required_path("_main/cluster/terraform/main/cilium-values.yaml").read_text())

    expected = _public_kubernetes_node_ips(hosts)
    assert expected, "nebula-mesh.json should contain at least one public Kubernetes node"
    smtp_service = _resource(service_yaml, "Service", "haku-mailbox-smtp")
    assert "externalIPs" not in smtp_service["spec"]
    assert smtp_service["spec"]["ports"] == [{"name": "smtp", "port": 2525, "targetPort": "smtp"}]

    daemonset = _resource(ingress_yaml, "DaemonSet", "haku-mailbox-smtp-ingress")
    pod_spec = daemonset["spec"]["template"]["spec"]
    gateway_selector = cilium_values["gatewayAPI"]["hostNetwork"]["nodes"]["matchLabels"]
    assert pod_spec["nodeSelector"] == gateway_selector
    smtp_container = one(container for container in pod_spec["containers"] if container["name"] == "nginx")
    smtp_port = one(port for port in smtp_container["ports"] if port["name"] == "smtp")
    assert smtp_port == {"name": "smtp", "containerPort": 2525, "hostPort": 25, "protocol": "TCP"}

    ingress_config = _resource(ingress_yaml, "ConfigMap", "haku-mailbox-smtp-ingress")["data"]["nginx.conf"]
    assert "proxy_protocol on;" in ingress_config
    assert "haku-mailbox-smtp.haku-mailbox.svc.cluster.local:2525" in ingress_config

    ingress_policy = _resource(ingress_yaml, "CiliumNetworkPolicy", "haku-mailbox-smtp-ingress")
    assert ingress_policy["spec"]["ingress"] == [
        {"fromEntities": ["world", "host"], "toPorts": [{"ports": [{"port": "2525", "protocol": "TCP"}]}]}
    ]

    mailbox_policy = _resource(ingress_yaml, "CiliumNetworkPolicy", "haku-mailbox")
    smtp_rule = one(
        rule
        for rule in mailbox_policy["spec"]["ingress"]
        if rule.get("fromEndpoints") == [{"matchLabels": {"app.kubernetes.io/name": "haku-mailbox-smtp-ingress"}}]
    )
    assert smtp_rule["fromEndpoints"] == [{"matchLabels": {"app.kubernetes.io/name": "haku-mailbox-smtp-ingress"}}]
    assert smtp_rule["toPorts"] == [{"ports": [{"port": "2525", "protocol": "TCP"}]}]


def test_api_dns_record_matches_public_control_plane_nodes() -> None:
    """api.allegedly.works should resolve to public control-plane nodes only."""
    hosts = _mesh_hosts(get_required_path("_main/nebula-mesh.json"))
    dns_records_tf = get_required_path("_main/tf/gitops/dns-records/main.tf")

    expected = _public_control_plane_ips(hosts)
    assert expected, "nebula-mesh.json should contain at least one public control-plane node"
    assert _terraform_local_ips(dns_records_tf, "kube_api_ips") == expected


if __name__ == "__main__":
    pytest_bazel.main()
