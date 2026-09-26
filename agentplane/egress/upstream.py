"""The upstream address: the admitted host, resolved by the proxy and pinned to the dial.

A policy names hosts, but a connection is made to an address, and nothing about a name says where
it points: an allowed name can resolve into the cluster (a Service, a Pod, a node), and it can
resolve differently on the second lookup (DNS rebinding). So the proxy resolves the host itself,
refuses every address that is not globally reachable, and dials exactly the address it checked.

The pin answers the dial's own name resolution, and the connection keeps the name its requests
carry: mitmproxy reuses an upstream connection only for requests to the address it carries, and
holds at most five open to one address per client connection. Re-addressed to its IP, a connection
is never reused, so every request on a kept-open tunnel dials another and the sixth waits forever.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Collection
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

from mitmproxy import connection
from more_itertools import unique_everseen

from agentplane.egress.policy import DenyReason

type Address = IPv4Address | IPv6Address
type Network = IPv4Network | IPv6Network


class UpstreamRefusedError(Exception):
    """The host has no address the proxy may dial; `reason` is what the client sees."""

    def __init__(self, reason: DenyReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def cluster_internal(address: Address) -> bool:
    """A private unicast address of the kind a cluster hands out: RFC1918 and ULA.

    Never loopback, link-local, multicast or unspecified. A rule declaring its host internal means
    the cluster network, and the Pod's own interfaces are not that: `127.0.0.1` is the sidecar and
    the runner's own listeners, and `169.254.169.254` is whatever the node's metadata service is.
    """
    return address.is_private and not (
        address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified
    )


def reachable(address: Address, exempt: Collection[Network], *, internal: bool = False) -> bool:
    """Globally reachable unicast by the IANA special-purpose registries, inside an exempt network,
    or -- when the deciding rule declared its host cluster-internal -- a private unicast address.

    An IPv4-mapped IPv6 address is judged as the IPv4 address it wraps, so `::ffff:10.0.0.1` is as
    private as `10.0.0.1` and an exemption of `127.0.0.0/8` covers `::ffff:127.0.0.1`.
    """
    unwrapped = (address.ipv4_mapped or address) if isinstance(address, IPv6Address) else address
    if any(unwrapped in network for network in exempt):
        return True
    if internal and cluster_internal(unwrapped):
        return True
    return unwrapped.is_global and not unwrapped.is_multicast


@dataclass(frozen=True)
class Pin:
    """The address a host was resolved to, valid for the dials made while it is fresh."""

    host: str
    port: int
    address: Address
    resolved_at: datetime


@dataclass(frozen=True)
class Dial:
    """A connection mitmproxy is about to make to `host:port`, and the socket address it goes to."""

    host: str
    port: int
    target: tuple[str, int]


_DIAL: ContextVar[Dial | None] = ContextVar("agentplane_egress_dial", default=None)


class UpstreamResolver:
    """Resolves and checks a host on admission, and answers the dial with the address checked.

    A pin lives `ttl`: admissions within it reuse the address without a lookup, which is what makes
    the dial go where the check looked, and a dial with no fresh pin has nothing to go to.
    """

    def __init__(
        self,
        *,
        exempt: Collection[Network] = (),
        ttl: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._exempt = exempt
        self._ttl = ttl
        self._clock = clock
        self._pins: dict[tuple[str, int], Pin] = {}

    def dial(self, server: connection.Server) -> None:
        """Answer the dial about to be made for `server` with the pin for its target, or kill it: a
        dial with no fresh pin did not come through the gate, which pins every target it admits just
        before the first bytes flow.

        Called from mitmproxy's `server_connect` hook, which runs in the task that then dials, so the
        answer reaches that dial's name resolution (`PinnedDialEventLoop`) and nothing else.
        """
        assert server.address is not None
        host, port = server.address[:2]
        pin = self.pinned(host, port)
        if pin is None:
            server.error = f"no pinned address for {host}:{port}"
            return
        _DIAL.set(Dial(host=pin.host, port=port, target=self._target(pin)))

    def _target(self, pin: Pin) -> tuple[str, int]:
        return str(pin.address), pin.port

    def pinned(self, host: str, port: int) -> Pin | None:
        pin = self._pins.get((host.lower(), port))
        return pin if pin is not None and self._fresh(pin, self._clock()) else None

    async def pin(self, host: str, port: int, *, internal: bool = False) -> Pin:
        """The fresh pin for the host, resolving it when there is none.

        A host with any address that is not reachable is refused whole, rather than served from
        its reachable addresses: a name that points into the cluster at all is not one a policy
        meant to admit -- unless the rule that admitted it says so, which is what `internal`
        carries. Among the reachable ones an IPv4 address is pinned before an IPv6 one: the Pod
        network the proxy dials from has no IPv6 route.
        """
        pin = self.pinned(host, port)
        if pin is not None:
            # The cache is keyed by host and port, not by who admitted it, so a private address
            # pinned for a rule that declared its host internal must not be served to one that did
            # not: the check runs again against the address already held.
            if not reachable(pin.address, self._exempt, internal=internal):
                raise UpstreamRefusedError(DenyReason.ADDRESS_FORBIDDEN, f"{host} is pinned to {pin.address}")
            return pin
        addresses = await self._resolve(host, port)
        forbidden = [address for address in addresses if not reachable(address, self._exempt, internal=internal)]
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


class PinnedDialEventLoop(asyncio.SelectorEventLoop):
    """The event loop the proxy runs on: a dial's name resolution returns the pinned address.

    mitmproxy connects with `asyncio.open_connection(host, port)`, which resolves `host` through
    the loop's `getaddrinfo` in the task that dials. There `UpstreamResolver.dial` has left the
    answer, which the first lookup takes; a lookup for any other name there is refused. Every other
    task resolves as usual.
    """

    async def getaddrinfo(
        self,
        host: bytes | str | None,
        port: bytes | str | int | None,
        *,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
        ]
    ]:
        dial = _DIAL.get()
        if dial is None:
            return await super().getaddrinfo(host, port, family=family, type=type, proto=proto, flags=flags)
        _DIAL.set(None)
        if not isinstance(host, str) or (host.lower(), port) != (dial.host, dial.port):
            raise socket.gaierror(
                socket.EAI_NONAME, f"{host!r}:{port!r} is not the pinned dial {dial.host}:{dial.port}"
            )
        version = ip_address(dial.target[0]).version
        return [
            (
                socket.AF_INET6 if version == 6 else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                dial.target,
            )
        ]
