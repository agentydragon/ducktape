import asyncio
import ipaddress
import socket
from collections.abc import Buffer
from contextvars import ContextVar

from mitmproxy import http
from mitmproxy.proxy.server_hooks import ServerConnectionHookData

checked_destinations: ContextVar[frozenset[tuple[str, int]] | None] = ContextVar("checked_destinations", default=None)


class OriginLoop(asyncio.SelectorEventLoop):
    async def sock_connect(self, sock: socket.socket, address: tuple[object, ...] | str | Buffer) -> None:
        if (checked := checked_destinations.get()) is not None:
            # asyncio resolves before this public hook. Check the actual numeric
            # dial target without changing mitmproxy's hostname-based pool key.
            if (
                not isinstance(address, tuple)
                or len(address) not in (2, 4)
                or not isinstance(address[0], str)
                or not isinstance(address[1], int)
                or (address[0], address[1]) not in checked
            ):
                raise OSError("Proxy destination changed after validation")
            ipaddress.ip_address(address[0])
        await super().sock_connect(sock, address)


def public_address(address: str) -> bool:
    parsed = ipaddress.ip_address(address)
    # is_global alone includes multicast, and IPv6 transition mechanisms can
    # conceal a different IPv4 destination from the policy check.
    return (
        parsed.is_global
        and not parsed.is_multicast
        and not parsed.is_reserved
        and not parsed.is_loopback
        and not parsed.is_link_local
        and not (
            isinstance(parsed, ipaddress.IPv6Address)
            and (
                parsed.ipv4_mapped is not None
                or parsed.sixtofour is not None
                or parsed.teredo is not None
                or parsed in ipaddress.IPv6Network("64:ff9b::/96")
            )
        )
    )


class PublicOrigins:
    def __init__(self, proxy_hostname: str) -> None:
        self.proxy_hostname = proxy_hostname.rstrip(".").lower()

    def permitted_authority(self, host: str, port: int) -> bool:
        return port in (80, 443) and host.rstrip(".").lower() != self.proxy_hostname

    def check_request(self, flow: http.HTTPFlow) -> None:
        # Authenticate runs first; preserve its denial and synthetic responses.
        if flow.response is None and not self.permitted_authority(flow.request.host, flow.request.port):
            flow.response = http.Response.make(403, b"Proxy destination not permitted\n")

    def http_connect(self, flow: http.HTTPFlow) -> None:
        self.check_request(flow)

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        self.check_request(flow)

    async def resolve(self, host: str, port: int) -> list[str]:
        results = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
        return list(dict.fromkeys(str(result[4][0]) for result in results))

    async def server_connect(self, data: ServerConnectionHookData) -> None:
        # Child connection tasks can inherit context from an older connection.
        checked_destinations.set(frozenset())
        server = data.server
        if server.error is not None:
            return
        if server.address is None or not self.permitted_authority(*server.address):
            server.error = "Proxy destination not permitted"
            return
        host, port = server.address
        try:
            async with asyncio.timeout(10):
                addresses = await self.resolve(host, port)
                own_addresses = await self.resolve(self.proxy_hostname, 443)
            if (
                not addresses
                or not own_addresses
                or any(not public_address(address) or address in own_addresses for address in addresses)
            ):
                server.error = "Proxy destination not permitted"
                return
        except (OSError, ValueError, TimeoutError):
            server.error = "Proxy destination resolution failed"
            return
        # Mitmproxy awaits this hook and its dial in the same connection task.
        checked_destinations.set(frozenset((address, port) for address in addresses))
