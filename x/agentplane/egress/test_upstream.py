"""The resolver's verdicts on addresses and names, and the dial's dependence on a fresh pin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network

import pytest
import pytest_bazel
from mitmproxy import connection

from x.agentplane.egress.policy import DenyReason
from x.agentplane.egress.upstream import UpstreamRefusedError, UpstreamResolver, reachable

LOOPBACK = frozenset({ip_network("127.0.0.0/8"), ip_network("::1/128")})


@pytest.mark.parametrize(
    "address",
    ["10.1.2.3", "172.16.0.1", "192.168.1.1", "100.64.0.1", "169.254.169.254", "127.0.0.1", "0.0.0.0", "224.0.0.1"],
)
def test_ipv4_special_purpose_ranges_are_not_reachable(address: str) -> None:
    assert not reachable(ip_address(address), frozenset())


@pytest.mark.parametrize("address", ["::1", "fc00::1", "fe80::1", "ff02::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1"])
def test_ipv6_special_purpose_ranges_are_not_reachable(address: str) -> None:
    assert not reachable(ip_address(address), frozenset())


@pytest.mark.parametrize("address", ["140.82.121.4", "2606:50c0:8000::153"])
def test_global_unicast_is_reachable(address: str) -> None:
    assert reachable(ip_address(address), frozenset())


def test_exemption_covers_the_ipv4_mapped_form() -> None:
    assert reachable(ip_address("::ffff:127.0.0.1"), LOOPBACK)
    assert not reachable(ip_address("::ffff:10.0.0.1"), LOOPBACK)


async def test_literal_address_is_checked_without_a_lookup() -> None:
    resolver = UpstreamResolver()
    assert (await resolver.pin("140.82.121.4", 443)).address == ip_address("140.82.121.4")
    with pytest.raises(UpstreamRefusedError) as refused:
        await resolver.pin("[::ffff:10.0.0.1]", 443)
    assert refused.value.reason is DenyReason.ADDRESS_FORBIDDEN


async def test_name_resolving_into_a_forbidden_range_is_refused() -> None:
    with pytest.raises(UpstreamRefusedError) as refused:
        await UpstreamResolver().pin("localhost", 443)
    assert refused.value.reason is DenyReason.ADDRESS_FORBIDDEN
    assert (await UpstreamResolver(exempt=LOOPBACK).pin("LocalHost", 443)).address == ip_address("127.0.0.1")


@pytest.mark.parametrize("address", ["10.1.2.3", "172.16.0.1", "192.168.1.1", "fd00::1"])
def test_a_rule_declaring_its_host_internal_reaches_a_private_address(address: str) -> None:
    """What lets an in-cluster Service be reached through the proxy at all."""
    assert not reachable(ip_address(address), frozenset())
    assert reachable(ip_address(address), frozenset(), internal=True)


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0", "::1"])
def test_declaring_a_host_internal_never_reaches_the_sandbox_s_own_interfaces(address: str) -> None:
    """`internal` means the cluster network. Loopback is the sidecar and the runner's own listeners,
    and link-local is whatever the node's metadata service is; neither is a Service."""
    assert not reachable(ip_address(address), frozenset(), internal=True)


async def test_a_private_address_pinned_for_an_internal_rule_is_not_served_to_another() -> None:
    """The pin cache is keyed by host and port, not by who admitted it. Without re-checking the
    address it holds, one rule declaring a host internal would lift the guard for every rule that
    names it while the pin stays fresh."""
    resolver = UpstreamResolver()
    pinned = await resolver.pin("10.1.2.3", 443, internal=True)

    assert pinned.address == ip_address("10.1.2.3")
    with pytest.raises(UpstreamRefusedError) as refused:
        await resolver.pin("10.1.2.3", 443)
    assert refused.value.reason is DenyReason.ADDRESS_FORBIDDEN


async def test_unresolvable_name_is_refused() -> None:
    with pytest.raises(UpstreamRefusedError) as refused:
        await UpstreamResolver().pin("nonexistent.invalid", 443)
    assert refused.value.reason is DenyReason.HOST_UNRESOLVED


async def test_pin_is_reused_while_fresh_and_not_after() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    resolver = UpstreamResolver(exempt=LOOPBACK, ttl=timedelta(seconds=30), clock=lambda: now)
    pin = await resolver.pin("localhost", 443)
    assert resolver.pinned("LOCALHOST", 443) == pin
    assert resolver.pinned("localhost", 80) is None
    now += timedelta(seconds=31)
    assert resolver.pinned("localhost", 443) is None


async def test_dial_goes_to_the_pinned_address_and_nowhere_without_one() -> None:
    resolver = UpstreamResolver(exempt=LOOPBACK)
    unpinned = connection.Server(address=("localhost", 443))
    resolver.redirect(unpinned)
    assert unpinned.error is not None
    assert unpinned.address == ("localhost", 443)
    await resolver.pin("localhost", 443)
    pinned = connection.Server(address=("localhost", 443))
    resolver.redirect(pinned)
    assert (pinned.error, pinned.address) == (None, ("127.0.0.1", 443))


if __name__ == "__main__":
    pytest_bazel.main()
