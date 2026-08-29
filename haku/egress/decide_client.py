"""The seam between the egress proxy and the Console decision endpoint."""

from __future__ import annotations

from abc import ABC, abstractmethod
from ipaddress import IPv4Address, IPv6Address

from haku.egress.decision import HttpAuthorizationDecision, RequestMeta


class DecideClient(ABC):
    """One decision call per request/CONNECT: reachability verdict plus substitutions.

    The gate resolves the request host once and picks the pin (addon.py); the client only
    transports that complete answer to the decision endpoint. Implementations raise rather
    than invent a verdict when a decision cannot be obtained — the gate addon turns any
    exception into a refusal (fail closed).
    """

    @abstractmethod
    async def decide(
        self,
        request: RequestMeta,
        *,
        resolved_ips: frozenset[IPv4Address | IPv6Address],
        upstream_ip: IPv4Address | IPv6Address,
        session_token: str,
    ) -> HttpAuthorizationDecision: ...
