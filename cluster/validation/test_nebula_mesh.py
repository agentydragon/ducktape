"""Validates the Nebula mesh roster (nebula-mesh.json)."""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_bazel
import yaml

from cluster.scripts import nebula_mesh
from cluster.validation.terraform_hcl import locals_blocks
from util.bazel.runfiles import get_required_path

# The `locals` maps in ovh-nodes.tf that carry per-node role/hostname/nebula_ip. pygohcl decodes
# string literals to plain Python values (it wraps HashiCorp's HCL2 parser), so these fields come
# out unquoted directly — no expression evaluation needed (the fields we read are literals).
_NODE_INVENTORY_LOCALS = ("kimsufi_servers", "kimsufi_cp_servers")


@pytest.fixture(scope="module")
def mesh() -> nebula_mesh.Mesh:
    return nebula_mesh.load(get_required_path("_main/nebula-mesh.json"))


def test_schema_loads(mesh: nebula_mesh.Mesh) -> None:
    """Roster parses against the Pydantic schema (Mesh.model_validate)."""
    assert mesh.hosts, "roster must contain at least one host"


def test_rugged_declares_conservative_destination_mtu(mesh: nebula_mesh.Mesh) -> None:
    """All peers must size traffic to fit Rugged's Google Fi underlay."""
    assert mesh.hosts["rugged"].destination_mtu == 1100


def test_global_mtu_consumer_uses_smallest_destination_constraint(mesh: nebula_mesh.Mesh) -> None:
    """Mobile clients without per-peer routes must honor the smallest constraint."""
    assert mesh.minimum_path_mtu(1300) == 1100


def test_global_mtu_consumer_keeps_default_without_constraints() -> None:
    """An unconstrained roster must not lower a consumer's normal MTU."""
    unconstrained_mesh = nebula_mesh.Mesh(
        hosts={"mobile": nebula_mesh.Host(nebula_ip="10.42.255.254", role="non-k8s", managed_by="mobile")}
    )
    assert unconstrained_mesh.minimum_path_mtu(1300) == 1300


@pytest.mark.parametrize(
    ("destination_mtu", "message"),
    [
        (nebula_mesh.MIN_DESTINATION_MTU - 1, "greater than or equal to 500"),
        (nebula_mesh.MESH_TUN_MTU + 1, "less than or equal to 1420"),
    ],
)
def test_destination_mtu_must_fit_nebula_route_limits(destination_mtu: int, message: str) -> None:
    """Reject route MTUs Nebula cannot install or that exceed the mesh TUN."""
    with pytest.raises(ValueError, match=message):
        nebula_mesh.Host(nebula_ip="10.42.255.254", role="non-k8s", managed_by="nixos", destination_mtu=destination_mtu)


def test_destination_mtu_must_be_omitted_instead_of_null() -> None:
    """Raw JSON consumers cannot safely treat an explicit null as an MTU."""
    with pytest.raises(ValueError, match="omit destination_mtu instead of setting it to null"):
        nebula_mesh.Host(nebula_ip="10.42.255.254", role="non-k8s", managed_by="nixos", destination_mtu=None)


@pytest.mark.parametrize("destination_mtu", ["1100", 1100.0])
def test_destination_mtu_must_be_a_strict_integer(destination_mtu: object) -> None:
    """Raw JSON consumers require integer route and advertised-MSS values."""
    with pytest.raises(ValueError, match="Input should be a valid integer"):
        nebula_mesh.Host.model_validate(
            {"nebula_ip": "10.42.255.254", "role": "non-k8s", "managed_by": "nixos", "destination_mtu": destination_mtu}
        )


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


def _terraform_control_plane_nebula_ips(path: Path) -> dict[str, str]:
    nodes: dict[str, dict[str, str]] = {}
    for block in locals_blocks(path):
        for local_name in _NODE_INVENTORY_LOCALS:
            nodes.update(block.get(local_name, {}))
    by_role: dict[str, dict[str, str]] = defaultdict(dict)
    for node in nodes.values():
        by_role[node["role"]][node["hostname"]] = node["nebula_ip"]
    control_planes = by_role["controlplane"]
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


def _hostexec_exec_url_nebula_ips(path: Path) -> dict[str, str]:
    """{host: exec_url host-component} from haku-console's `hostexec` host map (config.yaml)."""
    config = yaml.safe_load(path.read_text())
    hostexec = config.get("hostexec")
    assert hostexec is not None, f"{path}: expected a `hostexec` host map to cross-check against the mesh"
    ips: dict[str, str] = {}
    for name, entry in hostexec["hosts"].items():
        host = urlsplit(entry["exec_url"]).hostname
        assert host, f"{name}: exec_url {entry['exec_url']!r} has no host component"
        ips[name] = host
    return ips


def test_hostexec_exec_urls_match_mesh_nebula_ips(mesh: nebula_mesh.Mesh) -> None:
    """haku-console's `hostexec` host map addresses each in-scope machine by its Nebula IP — the same
    IP hostexecd binds to (nix/nixos/modules/hostexecd.nix derives it from this roster). So the
    exec_url host components must track nebula-mesh.json; a silent re-IP would strand the console.
    """
    hostexec_ips = _hostexec_exec_url_nebula_ips(get_required_path("_main/cluster/k8s/haku/console/config.yaml"))
    assert hostexec_ips, "hostexec host map must not be empty"
    for name, ip in hostexec_ips.items():
        assert name in mesh.hosts, f"hostexec host {name!r} is not in the mesh roster"
        assert ip == mesh.hosts[name].nebula_ip, (
            f"hostexec exec_url IP for {name!r} ({ip}) != mesh nebula_ip ({mesh.hosts[name].nebula_ip})"
        )


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
