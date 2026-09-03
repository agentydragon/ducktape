"""The upstream address: the admitted host, resolved by the proxy and pinned to the dial.

A policy names hosts, but a connection is made to an address, and nothing about a name says where
it points: an allowed name can resolve into the cluster (a Service, a Pod, a node), and it can
resolve differently on the second lookup (DNS rebinding). So the proxy resolves the host itself,
refuses every address that is not globally reachable, and dials exactly the address it checked.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

from mitmproxy import connection
from more_itertools import unique_everseen

from x.agentplane.egress.policy import DenyReason

type Address = IPv4Address | IPv6Address
type Network = IPv4Network | IPv6Network


class UpstreamRefusedError(Exception):
    """The host has no address the proxy may dial; `reason` is what the client sees."""

    def __init__(self, reason: DenyReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def reachable(address: Address, exempt: frozenset[Network]) -> bool:
    """Globally reachable unicast by the IANA special-purpose registries, or inside an exempt network.

    An IPv4-mapped IPv6 address is judged as the IPv4 address it wraps, so `::ffff:10.0.0.1` is as
    private as `10.0.0.1` and an exemption of `127.0.0.0/8` covers `::ffff:127.0.0.1`.
    """
    unwrapped = (address.ipv4_mapped or address) if isinstance(address, IPv6Address) else address
    if any(unwrapped in network for network in exempt):
        return True
    return unwrapped.is_global and not unwrapped.is_multicast


@dataclass(frozen=True)
class Pin:
    """The address a host was resolved to, valid for the dials made while it is fresh."""

    host: str
    port: int
    address: Address
    resolved_at: datetime


class UpstreamResolver:
    """Resolves and checks a host on admission, and answers the dial with the address checked.

    A pin lives `ttl`: admissions within it reuse the address without a lookup, which is what makes
    the dial go where the check looked, and a dial with no fresh pin has nothing to go to.
    """

    def __init__(
        self,
        *,
        exempt: frozenset[Network] = frozenset(),
        ttl: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._exempt = exempt
        self._ttl = ttl
        self._clock = clock
        self._pins: dict[tuple[str, int], Pin] = {}

    def redirect(self, server: connection.Server) -> None:
        """Point a dial at the pin for its target, or kill it: a dial with no fresh pin did not come
        through the gate, which pins every target it admits just before the first bytes flow."""
        assert server.address is not None
        host, port = server.address[:2]
        pin = self.pinned(host, port)
        if pin is None:
            server.error = f"no pinned address for {host}:{port}"
            return
        server.address = (str(pin.address), port)

    def pinned(self, host: str, port: int) -> Pin | None:
        pin = self._pins.get((host.lower(), port))
        return pin if pin is not None and self._fresh(pin, self._clock()) else None

    async def pin(self, host: str, port: int) -> Pin:
        """The fresh pin for the host, resolving it when there is none.

        A host with any address that is not reachable is refused whole, rather than served from
        its reachable addresses: a name that points into the cluster at all is not one a policy
        meant to admit. Among the reachable ones an IPv4 address is pinned before an IPv6 one:
        the Pod network the proxy dials from has no IPv6 route.
        """
        pin = self.pinned(host, port)
        if pin is not None:
            return pin
        addresses = await self._resolve(host, port)
        forbidden = [address for address in addresses if not reachable(address, self._exempt)]
        if forbidden:
            raise UpstreamRefusedError(DenyReason.ADDRESS_FORBIDDEN, f"{host} resolves to {forbidden[0]}")
        now = self._clock()
        address = min(addresses, key=lambda candidate: candidate.version)  # stable: first of the family
        pin = Pin(host=host.lower(), port=port, address=address, resolved_at=now)
        self._pins = {key: kept for key, kept in self._pins.items() if self._fresh(kept, now)}
        self._pins[pin.host, port] = pin
        return pin

    def _fresh(self, pin: Pin, now: datetime) -> bool:
        return now < pin.resolved_at + self._ttl

    async def _resolve(self, host: str, port: int) -> list[Address]:
        try:
            return [ip_address(host.strip("[]"))]  # a literal is checked, never looked up
        except ValueError:
            pass
        try:
            found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise UpstreamRefusedError(DenyReason.HOST_UNRESOLVED, f"{host}: {error}") from error
        addresses = list(unique_everseen(ip_address(sockaddr[0]) for _family, _type, _proto, _name, sockaddr in found))
        if not addresses:
            raise UpstreamRefusedError(DenyReason.HOST_UNRESOLVED, f"{host}: no addresses")
        return addresses
