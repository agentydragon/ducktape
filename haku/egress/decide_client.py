"""The seam between the egress proxy and the Console decision endpoint."""

from __future__ import annotations

from abc import ABC, abstractmethod

from haku.egress.decision import DecideResponse, RequestMeta


class DecideClient(ABC):
    """One decision call per request/CONNECT: reachability verdict plus substitutions.

    Implementations raise rather than invent a verdict when a decision cannot be
    obtained — the gate addon turns any exception into a refusal (fail closed).
    """

    @abstractmethod
    async def decide(self, request: RequestMeta) -> DecideResponse: ...
