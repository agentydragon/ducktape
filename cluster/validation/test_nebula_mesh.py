"""Validates the Nebula mesh roster (nebula-mesh.json)."""

from __future__ import annotations

import ipaddress

import pytest
import pytest_bazel
import yaml
from pydantic import ValidationError

from cluster.scripts import nebula_mesh, render_mobile_nebula_config

# gazelle:include_dep //cluster/scripts:nebula_mesh_fixtures
pytest_plugins = ("cluster.scripts.nebula_mesh_fixtures",)


@pytest.mark.parametrize(("limits", "expected"), [((), 1300), ((1400,), 1300), ((1200, 1100), 1100)])
def test_mobile_tun_mtu_honors_the_smallest_path_limit(limits: tuple[int, ...], expected: int) -> None:
    hosts = {"phone": nebula_mesh.Host(nebula_ip="10.42.0.1", role="non-k8s", managed_by="mobile")}
    for i, limit in enumerate(limits, start=2):
        hosts[f"peer-{i}"] = nebula_mesh.Host(
            nebula_ip=f"10.42.0.{i}", role="non-k8s", managed_by="nixos", destination_mtu=limit
        )
    config = render_mobile_nebula_config.render_config(
        ca="test-ca", cert="test-cert", key="test-key", mesh=nebula_mesh.Mesh(hosts=hosts), include_dns=False
    )
    assert yaml.safe_load(config)["tun"]["mtu"] == expected


@pytest.mark.parametrize("destination_mtu", [499, 1421, None, "1100", 1100.0])
def test_destination_mtu_requires_an_integer_within_nebula_route_limits(destination_mtu: object) -> None:
    """JSON consumers distinguish an omitted override from null and require an integer in [500, 1420]."""
    with pytest.raises(ValidationError) as rejected:
        nebula_mesh.Host.model_validate(
            {"nebula_ip": "10.42.0.2", "role": "non-k8s", "managed_by": "nixos", "destination_mtu": destination_mtu}
        )
    assert [error["loc"] for error in rejected.value.errors()] == [("destination_mtu",)]


def test_nebula_ips_are_valid_and_unique(nebula_roster: nebula_mesh.Mesh) -> None:
    """nebula_ip must be a valid IPv4 in 10.42.0.0/16 and unique across hosts."""
    seen: dict[str, str] = {}
    for name, host in nebula_roster.hosts.items():
        addr = ipaddress.IPv4Address(host.nebula_ip)
        assert addr in ipaddress.IPv4Network("10.42.0.0/16"), f"{name}: nebula_ip {host.nebula_ip} outside 10.42.0.0/16"
        assert host.nebula_ip not in seen, f"duplicate nebula_ip {host.nebula_ip}: {seen[host.nebula_ip]} vs {name}"
        seen[host.nebula_ip] = name


def test_endpoints_are_host_port(nebula_roster: nebula_mesh.Mesh) -> None:
    """Every endpoint parses as <ip-or-host>:4242, the port nebula.tf's drift check builds live endpoints with."""
    for name, host in nebula_roster.hosts.items():
        if host.endpoint is None:
            continue
        head, _, tail = host.endpoint.rpartition(":")
        assert head, f"{name}: endpoint {host.endpoint!r} must be host:port"
        assert tail == "4242", f"{name}: endpoint {host.endpoint!r} must use the Nebula public port"


def test_lighthouses_have_endpoints(nebula_roster: nebula_mesh.Mesh) -> None:
    """A lighthouse must be reachable — i.e. have a public endpoint."""
    for name, host in nebula_roster.hosts.items():
        if host.lighthouse:
            assert host.endpoint is not None, f"{name}: lighthouse=true requires endpoint"


def test_at_least_two_reachable_lighthouses(nebula_roster: nebula_mesh.Mesh) -> None:
    """Roaming/laptop hosts need ≥2 lighthouses with public endpoints to avoid SPOF."""
    reachable_lighthouses = [h for h in nebula_roster.lighthouses() if h.endpoint is not None]
    assert len(reachable_lighthouses) >= 2, (
        f"need ≥2 reachable lighthouses, found {len(reachable_lighthouses)}: "
        f"{[h.nebula_ip for h in reachable_lighthouses]}"
    )


def test_at_least_one_control_plane(nebula_roster: nebula_mesh.Mesh) -> None:
    """k8s-worker.nix derives controlPlaneEndpoints from role=control-plane."""
    assert nebula_roster.control_planes(), "roster must contain at least one role=control-plane host"


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


def test_host_names_have_no_dots(nebula_roster: nebula_mesh.Mesh) -> None:
    """Host names must be single DNS labels (no dots).

    Talos's HostnameConfig accepts FQDN-shaped strings but splits them at the
    first dot into hostname + domainname when writing to the kernel. Kubelet
    then registers the node under the (short) hostname only, so a dotted host
    name in this roster registers in Kubernetes under a truncated name and
    breaks every downstream consumer (local-path-provisioner nodePathMap,
    nodeSelector pins, etc.).
    """
    for name in nebula_roster.hosts:
        assert "." not in name, (
            f"host name {name!r} contains a dot; Talos splits at the first dot, "
            f"so kubelet would register the node as {name.split('.', 1)[0]!r}. "
            "Use a single DNS label."
        )


if __name__ == "__main__":
    pytest_bazel.main()
