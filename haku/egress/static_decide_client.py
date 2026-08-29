"""In-memory decide client for tests: fixed decision, recorded requests."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

from haku.egress.decide_client import DecideClient
from haku.egress.decision import DecideResponse, RequestMeta


class StaticDecideClient(DecideClient):
    """Returns ``decision`` for every request and records what was asked."""

    def __init__(self, decision: DecideResponse) -> None:
        self._decision = decision
        self.requests: list[RequestMeta] = []

    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
        proxy_client_credential: str,
    ) -> DecideResponse:
        del resolved_ips, upstream_ip, proxy_client_credential
        self.requests.append(request)
        return self._decision
