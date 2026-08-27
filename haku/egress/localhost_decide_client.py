"""Client of the colocated Console decision endpoint (github.com/agentydragon/ducktape/issues/4670).

Speaks ``POST /api/internal/http/decide`` (haku/console/http_decide_routes.py):
the static proxy identity bearer travels in ``Authorization``, the Agent-bound
fence credential inside the ``DecideRequest`` body. Any failure — connection
error, timeout, non-2xx (401 rejected bearer, 503 unconfigured or authority
failure), unparseable body — raises instead of inventing a verdict; the gate
addon turns every raise into a refusal, so the proxy fails closed.
"""

from __future__ import annotations

import asyncio
import socket
from ipaddress import IPv4Address, IPv6Address, ip_address

import httpx
from pydantic import SecretStr, TypeAdapter

from haku.egress.decide_client import DecideClient
from haku.egress.decision import DecideRequest, DecideResponse, RequestMeta

DECIDE_PATH = "/api/internal/http/decide"
DEFAULT_TIMEOUT_SECONDS = 5.0

_RESPONSE_ADAPTER: TypeAdapter[DecideResponse] = TypeAdapter(DecideResponse)


async def _resolve(host: str, port: int) -> frozenset[IPv4Address | IPv6Address]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return frozenset(ip_address(sockaddr[0]) for _family, _type, _proto, _canonname, sockaddr in infos)


class LocalhostDecideClient(DecideClient):
    """One decide POST per request/CONNECT; owns its ``httpx`` client (``aclose`` on shutdown)."""

    def __init__(
        self,
        *,
        base_url: str,
        proxy_bearer: SecretStr,
        fence_credential: SecretStr,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._fence_credential = fence_credential
        # trust_env=False: a localhost machine-to-machine hop must never route
        # through HTTP(S)_PROXY from the environment.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {proxy_bearer.get_secret_value()}"},
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def decide(self, request: RequestMeta) -> DecideResponse:
        resolved = await _resolve(request.host, request.port)
        decide_request = DecideRequest(
            fence_credential=self._fence_credential,
            request=request,
            resolved_ips=resolved,
            # Deterministic pin from the validated answer, in wire serialization
            # order (IPv4 before IPv6, then numeric).
            # TODO(github.com/agentydragon/ducktape/issues/4670): enforce the pin —
            # mitmproxy still re-resolves the hostname when dialing the upstream.
            upstream_ip=min(resolved, key=lambda address: (address.version, int(address))),
        )
        response = await self._client.post(
            DECIDE_PATH, content=decide_request.model_dump_json(), headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        return _RESPONSE_ADAPTER.validate_json(response.content)

    async def aclose(self) -> None:
        await self._client.aclose()
