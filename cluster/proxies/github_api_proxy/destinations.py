import asyncio
import ipaddress
import socket

from mitmproxy import http
from mitmproxy.proxy.server_hooks import ServerConnectionHookData


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
        # server_connect precedes asyncio.open_connection. Pin the checked numeric
        # result so DNS cannot change between validation and the actual socket dial.
        # Preserve certificate identity even though the transport now uses an IP.
        if server.tls and server.sni is None:
            server.sni = host
        server.address = (addresses[0], port)
