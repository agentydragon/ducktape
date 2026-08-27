"""In-memory decide client for tests: fixed decision, recorded requests."""

from __future__ import annotations

from haku.egress.decide_client import DecideClient
from haku.egress.decision import DecideResponse, RequestMeta


class StaticDecideClient(DecideClient):
    """Returns ``decision`` for every request and records what was asked."""

    def __init__(self, decision: DecideResponse) -> None:
        self._decision = decision
        self.requests: list[RequestMeta] = []

    async def decide(self, request: RequestMeta) -> DecideResponse:
        self.requests.append(request)
        return self._decision
