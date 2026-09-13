import asyncio
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from mitmproxy import http
from mitmproxy.proxy.server_hooks import ServerConnectionHookData

_ProtocolT = TypeVar("_ProtocolT", bound=asyncio.BaseProtocol)


@dataclass(frozen=True)
class OriginPolicy:
    """The DNS and address policy for outbound web-origin connections."""

    proxy_hostname: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "proxy_hostname", self.proxy_hostname.rstrip(".").lower())

    def permitted_authority(self, host: str, port: int) -> bool:
        return port in (80, 443) and host.rstrip(".").lower() != self.proxy_hostname

    async def resolve(self, host: str, port: int, *, family: int = 0) -> list[str]:
        results = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
        return list(dict.fromkeys(str(result[4][0]) for result in results))

    async def own_addresses(self) -> list[str]:
        try:
            async with asyncio.timeout(10):
                return await self.resolve(self.proxy_hostname, 443)
        except (OSError, ValueError, TimeoutError) as exc:
            raise OSError("Proxy destination resolution failed") from exc

    async def select_address(self, host: str, port: int, *, family: int = 0) -> str:
        if not self.permitted_authority(host, port):
            raise OSError("Proxy destination not permitted")
        try:
            async with asyncio.timeout(10):
                addresses = await self.resolve(host, port, family=family)
                own_addresses = await self.resolve(self.proxy_hostname, 443)
        except (OSError, ValueError, TimeoutError) as exc:
            raise OSError("Proxy destination resolution failed") from exc
        try:
            normalized_own = {str(ipaddress.ip_address(address)) for address in own_addresses}
            permitted = (
                bool(addresses)
                and bool(normalized_own)
                and all(
                    public_address(address) and str(ipaddress.ip_address(address)) not in normalized_own
                    for address in addresses
                )
            )
        except ValueError as exc:
            raise OSError("Proxy destination resolution failed") from exc
        if not permitted:
            raise OSError("Proxy destination not permitted")
        return addresses[0]


class OriginLoop(asyncio.SelectorEventLoop):
    def __init__(self, proxy_hostname: str) -> None:
        super().__init__()
        self.origin_policy = OriginPolicy(proxy_hostname)

    async def create_connection(
        self, protocol_factory: Callable[[], _ProtocolT], host: Any = None, port: Any = None, *args: Any, **kwargs: Any
    ) -> tuple[asyncio.Transport, _ProtocolT]:
        # The pinned mitmproxy API has one `Server.address` for both the logical
        # pool key and asyncio's dial input. Keep that logical hostname intact and
        # adapt only the latter at this public boundary; see README.md.
        if kwargs.get("sock") is None and isinstance(host, (str, bytes)) and isinstance(port, int):
            try:
                logical_host = host.decode("idna") if isinstance(host, bytes) else host
            except UnicodeError as exc:
                raise OSError("Proxy destination resolution failed") from exc
            if self.origin_policy.permitted_authority(logical_host, port):
                try:
                    ipaddress.ip_address(logical_host)
                except ValueError:
                    host = await self.origin_policy.select_address(logical_host, port, family=kwargs.get("family", 0))
                    if kwargs.get("ssl") and kwargs.get("server_hostname") is None:
                        kwargs["server_hostname"] = logical_host

        return await super().create_connection(protocol_factory, host, port, *args, **kwargs)


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
    def __init__(self, policy: OriginPolicy) -> None:
        self.policy = policy

    def check_request(self, flow: http.HTTPFlow) -> None:
        # Authenticate runs first; preserve its denial and synthetic responses.
        if flow.response is None and not self.policy.permitted_authority(flow.request.host, flow.request.port):
            flow.response = http.Response.make(403, b"Proxy destination not permitted\n")

    def http_connect(self, flow: http.HTTPFlow) -> None:
        self.check_request(flow)

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        self.check_request(flow)

    async def server_connect(self, data: ServerConnectionHookData) -> None:
        server = data.server
        if server.error is not None:
            return
        if server.address is None or not self.policy.permitted_authority(*server.address):
            server.error = "Proxy destination not permitted"
            return
        host, _ = server.address
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Hostnames are resolved and validated by OriginLoop.create_connection,
            # immediately before asyncio creates the socket.
            return
        try:
            own_addresses = await self.policy.own_addresses()
        except OSError as exc:
            server.error = str(exc)
            return
        try:
            normalized_host = str(ipaddress.ip_address(host))
            normalized_own = {str(ipaddress.ip_address(address)) for address in own_addresses}
        except ValueError:
            server.error = "Proxy destination resolution failed"
            return
        if not own_addresses or not public_address(host) or normalized_host in normalized_own:
            server.error = "Proxy destination not permitted"
