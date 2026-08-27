"""Placeholder for the localhost-bound Console decision endpoint client."""

from __future__ import annotations

from haku.egress.decide_client import DecideClient
from haku.egress.decision import Decision, RequestMeta


class LocalhostDecideClient(DecideClient):
    """Will call the colocated Console decision endpoint once it exists.

    Until then every decide raises, which the gate addon turns into a refusal:
    a proxy wired to this client refuses all egress rather than failing open.
    """

    async def decide(self, request: RequestMeta) -> Decision:
        raise NotImplementedError(
            "Console decision endpoint is not implemented yet; see https://github.com/agentydragon/ducktape/issues/4670"
        )
