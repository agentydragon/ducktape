"""Validates public DNS records against the cluster node roster."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest_bazel

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


def test_api_dns_record_matches_public_control_plane_nodes() -> None:
    """api.allegedly.works should resolve to public control-plane nodes only."""
    hosts = _mesh_hosts(get_required_path("_main/nebula-mesh.json"))
    dns_records_tf = get_required_path("_main/tf/gitops/dns-records/main.tf")

    expected = _public_control_plane_ips(hosts)
    assert expected, "nebula-mesh.json should contain at least one public control-plane node"
    assert _terraform_local_ips(dns_records_tf, "kube_api_ips") == expected


if __name__ == "__main__":
    pytest_bazel.main()
