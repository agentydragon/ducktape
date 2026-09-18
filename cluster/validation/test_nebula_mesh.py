"""Validates the Nebula mesh roster (nebula-mesh.json)."""

from __future__ import annotations

import ipaddress

import pytest
import pytest_bazel

from cluster.scripts import nebula_mesh
from util.bazel.runfiles import get_required_path


@pytest.fixture(scope="module")
def mesh() -> nebula_mesh.Mesh:
    return nebula_mesh.load(get_required_path("_main/nebula-mesh.json"))


def test_schema_loads(mesh: nebula_mesh.Mesh) -> None:
    """Roster parses against the Pydantic schema (Mesh.model_validate)."""
    assert mesh.hosts, "roster must contain at least one host"


def test_global_mtu_consumer_uses_smallest_destination_constraint() -> None:
    """Mobile clients without per-peer routes must honor the smallest constraint."""
    constrained_mesh = nebula_mesh.Mesh(
        hosts={
            "slow": nebula_mesh.Host(
                nebula_ip="10.42.255.253", role="non-k8s", managed_by="mobile", destination_mtu=1100
            ),
            "fast": nebula_mesh.Host(
                nebula_ip="10.42.255.254", role="non-k8s", managed_by="mobile", destination_mtu=1300
            ),
        }
    )
    assert constrained_mesh.minimum_path_mtu(1400) == 1100


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
    """Every endpoint parses as <ip-or-host>:4242, the port nebula.tf's drift check builds live endpoints with."""
    for name, host in mesh.hosts.items():
        if host.endpoint is None:
            continue
        head, _, tail = host.endpoint.rpartition(":")
        assert head, f"{name}: endpoint {host.endpoint!r} must be host:port"
        assert tail == "4242", f"{name}: endpoint {host.endpoint!r} must use the Nebula public port"


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


def test_public_kubernetes_nodes_are_reachable_cluster_members() -> None:
    """DNS and the etcd scrape follow these projections: every k8s node with a public
    endpoint, and every control plane."""
    hosts = {
        "cp": nebula_mesh.Host(
            nebula_ip="10.42.255.1", endpoint="203.0.113.1:4242", role="control-plane", managed_by="tofu-ovh"
        ),
        "worker": nebula_mesh.Host(
            nebula_ip="10.42.255.2", endpoint="203.0.113.2:4242", role="worker", managed_by="tofu-ovh"
        ),
        "home-worker": nebula_mesh.Host(nebula_ip="10.42.255.3", role="worker", managed_by="tofu-home"),
        "relay": nebula_mesh.Host(
            nebula_ip="10.42.255.4", endpoint="203.0.113.4:4242", role="non-k8s", managed_by="ansible"
        ),
    }
    mesh = nebula_mesh.Mesh(hosts=hosts)
    assert {name: host.public_ip for name, host in mesh.public_kubernetes_nodes().items()} == {
        "cp": "203.0.113.1",
        "worker": "203.0.113.2",
    }
    assert mesh.control_planes() == {"cp": hosts["cp"]}


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
