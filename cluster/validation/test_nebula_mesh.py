"""Validates the Nebula mesh roster (nebula-mesh.json)."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from cluster.scripts import nebula_mesh
from util.bazel.runfiles import get_required_path

_TERRAFORM_NODE_RE = re.compile(r"^    (?P<key>[A-Za-z0-9_]+)\s*=\s*\{(?P<body>.*?)^    \}", re.MULTILINE | re.DOTALL)
_TERRAFORM_STRING_ATTR_RE = re.compile(r'^\s*(?P<name>[A-Za-z0-9_]+)\s*=\s*"(?P<value>[^"]*)"', re.MULTILINE)


@pytest.fixture(scope="module")
def mesh() -> nebula_mesh.Mesh:
    return nebula_mesh.load(get_required_path("_main/nebula-mesh.json"))


def test_schema_loads(mesh: nebula_mesh.Mesh) -> None:
    """Roster parses against the Pydantic schema (Mesh.model_validate)."""
    assert mesh.hosts, "roster must contain at least one host"


def test_nebula_ips_are_valid_and_unique(mesh: nebula_mesh.Mesh) -> None:
    """nebula_ip must be a valid IPv4 in 10.42.0.0/16 and unique across hosts."""
    seen: dict[str, str] = {}
    for name, host in mesh.hosts.items():
        addr = ipaddress.IPv4Address(host.nebula_ip)
        assert addr in ipaddress.IPv4Network("10.42.0.0/16"), f"{name}: nebula_ip {host.nebula_ip} outside 10.42.0.0/16"
        assert host.nebula_ip not in seen, f"duplicate nebula_ip {host.nebula_ip}: {seen[host.nebula_ip]} vs {name}"
        seen[host.nebula_ip] = name


def test_endpoints_are_host_port(mesh: nebula_mesh.Mesh) -> None:
    """Every endpoint parses as <ip-or-host>:<port>."""
    for name, host in mesh.hosts.items():
        if host.endpoint is None:
            continue
        head, _, tail = host.endpoint.rpartition(":")
        assert head, f"{name}: endpoint {host.endpoint!r} must be host:port"
        assert tail.isdigit(), f"{name}: endpoint {host.endpoint!r} must be host:port"
        port = int(tail)
        assert 1 <= port <= 65535, f"{name}: endpoint port {port} out of range"


def test_lighthouses_have_endpoints(mesh: nebula_mesh.Mesh) -> None:
    """A lighthouse must be reachable — i.e. have a public endpoint."""
    for name, host in mesh.hosts.items():
        if host.lighthouse:
            assert host.endpoint is not None, f"{name}: lighthouse=true requires endpoint"


def test_at_least_two_reachable_lighthouses(mesh: nebula_mesh.Mesh) -> None:
    """Roaming/laptop hosts need ≥2 lighthouses with public endpoints to avoid SPOF."""
    reachable_lighthouses = [h for h in mesh.lighthouses() if h.endpoint is not None]
    assert len(reachable_lighthouses) >= 2, (
        f"need ≥2 reachable lighthouses, found {len(reachable_lighthouses)}: "
        f"{[h.nebula_ip for h in reachable_lighthouses]}"
    )


def test_at_least_one_control_plane(mesh: nebula_mesh.Mesh) -> None:
    """k8s-worker.nix derives controlPlaneEndpoints from role=control-plane."""
    cps = [h for h in mesh.hosts.values() if h.role == "control-plane"]
    assert cps, "roster must contain at least one role=control-plane host"


def _control_plane_nebula_ips(mesh: nebula_mesh.Mesh) -> dict[str, str]:
    return {name: host.nebula_ip for name, host in mesh.hosts.items() if host.role == "control-plane"}


def _string_attrs(terraform_block: str) -> dict[str, str]:
    return {match.group("name"): match.group("value") for match in _TERRAFORM_STRING_ATTR_RE.finditer(terraform_block)}


def _terraform_control_plane_nebula_ips(path: Path) -> dict[str, str]:
    text = path.read_text()
    control_planes: dict[str, str] = {}
    for match in _TERRAFORM_NODE_RE.finditer(text):
        attrs = _string_attrs(match.group("body"))
        if attrs.get("role") != "controlplane":
            continue
        hostname = attrs.get("hostname")
        nebula_ip = attrs.get("nebula_ip")
        assert hostname, f"{match.group('key')}: control-plane node is missing hostname"
        assert nebula_ip, f"{match.group('key')}: control-plane node is missing nebula_ip"
        control_planes[hostname] = nebula_ip
    assert control_planes, f"{path}: expected at least one control-plane node"
    return control_planes


def _static_etcd_endpoint_nebula_ips(path: Path) -> dict[str, str]:
    docs = [doc for doc in yaml.safe_load_all(path.read_text()) if doc is not None]
    endpoint_slices = [
        doc
        for doc in docs
        if doc.get("apiVersion") == "discovery.k8s.io/v1"
        and doc.get("kind") == "EndpointSlice"
        and doc.get("metadata", {}).get("name") == "talos-etcd-metrics"
    ]
    assert len(endpoint_slices) == 1, f"{path}: expected exactly one talos-etcd-metrics EndpointSlice"

    endpoint_slice = endpoint_slices[0]
    assert endpoint_slice.get("addressType") == "IPv4"
    assert (
        endpoint_slice.get("metadata", {}).get("labels", {}).get("kubernetes.io/service-name") == "talos-etcd-metrics"
    )
    assert endpoint_slice.get("ports") == [{"name": "metrics", "protocol": "TCP", "port": 2381}]

    endpoints: dict[str, str] = {}
    for endpoint in endpoint_slice.get("endpoints", []):
        hostname = endpoint.get("hostname")
        assert hostname, f"{path}: EndpointSlice endpoint is missing hostname"
        assert endpoint.get("nodeName") == hostname, f"{hostname}: endpoint nodeName should match hostname"
        assert endpoint.get("conditions", {}).get("ready") is True, f"{hostname}: endpoint should be marked ready"

        addresses = endpoint.get("addresses")
        assert isinstance(addresses, list), f"{hostname}: endpoint addresses must be a list"
        assert len(addresses) == 1, f"{hostname}: expected exactly one endpoint address, got {addresses!r}"
        assert hostname not in endpoints, f"{path}: duplicate endpoint hostname {hostname}"
        endpoints[hostname] = addresses[0]

    assert endpoints, f"{path}: expected at least one endpoint"
    return endpoints


def test_etcd_metrics_static_endpoints_match_control_plane_rosters(mesh: nebula_mesh.Mesh) -> None:
    """The static etcd scrape endpoints must follow the control-plane node inventories."""
    mesh_control_planes = _control_plane_nebula_ips(mesh)
    terraform_control_planes = _terraform_control_plane_nebula_ips(
        get_required_path("_main/cluster/terraform/main/ovh-nodes.tf")
    )
    static_etcd_endpoints = _static_etcd_endpoint_nebula_ips(
        get_required_path("_main/cluster/k8s/monitoring/etcd/endpoints.yaml")
    )

    assert terraform_control_planes == mesh_control_planes
    assert static_etcd_endpoints == mesh_control_planes


def test_host_names_have_no_dots(mesh: nebula_mesh.Mesh) -> None:
    """Host names must be single DNS labels (no dots).

    Talos's HostnameConfig accepts FQDN-shaped strings but splits them at the
    first dot into hostname + domainname when writing to the kernel. Kubelet
    then registers the node under the (short) hostname only, so a dotted host
    name in this roster registers in Kubernetes under a truncated name and
    breaks every downstream consumer (local-path-provisioner nodePathMap,
    nodeSelector pins, etc.). See plans/rename_ovh_nodes_role_neutral.md.
    """
    for name in mesh.hosts:
        assert "." not in name, (
            f"host name {name!r} contains a dot; Talos splits at the first dot, "
            f"so kubelet would register the node as {name.split('.', 1)[0]!r}. "
            "Use a single DNS label."
        )


if __name__ == "__main__":
    pytest_bazel.main()
