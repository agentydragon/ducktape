"""Client of the colocated Console decision endpoint (github.com/agentydragon/ducktape/issues/4670).

Speaks ``POST /api/internal/http/decide`` (haku/console/grants/http/decide_routes.py):
the shared-fence credential travels in ``Authorization``, and the caller's session
token travels inside the ``DecideRequest`` body; the gate's resolution and pin arrive as
arguments and travel verbatim. Any failure — connection error,
timeout, non-2xx (401 rejected bearer, 503 unconfigured or authority failure),
unparseable body — raises instead of inventing a verdict; the gate addon turns
every raise into a refusal, so the proxy fails closed.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

import httpx
from pydantic import SecretStr, TypeAdapter

from haku.egress.decide_client import DecideClient
from haku.egress.decision import DecideRequest, HttpAuthorizationDecision, RequestMeta

DECIDE_PATH = "/api/internal/http/decide"
DEFAULT_TIMEOUT_SECONDS = 5.0

_RESPONSE_ADAPTER: TypeAdapter[HttpAuthorizationDecision] = TypeAdapter(HttpAuthorizationDecision)


class LocalhostDecideClient(DecideClient):
    """One decide POST per request/CONNECT; owns its ``httpx`` client (``aclose`` on shutdown)."""

    def __init__(
        self, *, base_url: str, fence_credential: SecretStr, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        # trust_env=False: a localhost machine-to-machine hop must never route
        # through HTTP(S)_PROXY from the environment.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {fence_credential.get_secret_value()}"},
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
        session_token: str,
    ) -> HttpAuthorizationDecision:
        decide_request = DecideRequest(
            session_token=SecretStr(session_token), request=request, resolved_ips=resolved_ips, upstream_ip=upstream_ip
        )
        response = await self._client.post(
            DECIDE_PATH, content=decide_request.model_dump_json(), headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        return _RESPONSE_ADAPTER.validate_json(response.content)

    async def aclose(self) -> None:
        await self._client.aclose()
