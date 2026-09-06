"""The agent-facing egress rules API, separated from the proxy transport that currently hosts it.

The projection boundary accepts only the Sandbox identity already proven by the transport and
returns the deliberately redacted model from ``agent_view``. It does not accept a resource object,
request headers, or a request body as identity input. A future destination service can put its
``SandboxPrincipal`` through the same ``for_sandbox`` seam after authenticating the ordinary
Authorization bearer at that destination.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from x.agentplane.egress.agent_view import AgentEgressView, agent_view
from x.agentplane.egress.policy import Index

HOST = "agentplane-egress.agentplane-staging.svc.cluster.local"
PATH = "/v1/rules"


class SandboxNotCurrentError(Exception):
    """The proven Sandbox no longer exists under the same UID in the current policy index."""


class RulesProjection:
    """Project one current Sandbox's effective rules without exposing source resources."""

    def __init__(self, index: Index, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._index = index
        self._clock = clock

    def for_sandbox(self, sandbox_name: str, sandbox_uid: str) -> AgentEgressView:
        sandbox = self._index.sandboxes.get(sandbox_name)
        if sandbox is None or sandbox.metadata.uid != sandbox_uid:
            raise SandboxNotCurrentError(sandbox_name)
        return agent_view(self._index, sandbox, self._clock())


@dataclass(frozen=True)
class RulesResponse:
    status: int
    body: bytes
    content_type: str


class RulesApi:
    """The locally dispatched Service-DNS host/path contract over the projection backend."""

    def __init__(self, projection: RulesProjection) -> None:
        self._projection = projection

    @staticmethod
    def serves(host: str) -> bool:
        return host.lower() == HOST

    def request(self, path: str, *, sandbox_name: str, sandbox_uid: str) -> RulesResponse:
        if path != PATH:
            return RulesResponse(status=404, body=b"", content_type="text/plain")
        view = self._projection.for_sandbox(sandbox_name, sandbox_uid)
        return RulesResponse(status=200, body=view.model_dump_json().encode(), content_type="application/json")
