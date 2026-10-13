"""Destination-authenticated HTTP API for a Sandbox's redacted effective egress rules."""

from __future__ import annotations

from fastapi import FastAPI, Request

from agentplane.egress.agent_view import AgentEgressView, agent_view
from agentplane.egress.policy import Index
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from agentplane.workload_auth.principal import WorkloadPrincipal

PATH = "/v1/rules"


class RulesProjection:
    """Project one caller's effective rules without exposing source resources."""

    def __init__(self, index: Index) -> None:
        self._index = index

    def for_caller(self, principal: WorkloadPrincipal) -> AgentEgressView:
        """The view of the ServiceAccount the bearer proved. Nothing here can go stale between
        authentication and projection: the subject is the token's, not an object in the index."""
        return agent_view(
            self._index, ServiceAccountRef(namespace=principal.namespace, name=principal.service_account_name)
        )


def create_rules_app(authenticate: WorkloadPrincipalAuthenticator, projection: RulesProjection) -> FastAPI:
    """Create the ordinary destination API; request metadata is never an identity authority."""
    app = FastAPI(title="agentplane-egress-rules")

    @app.get(PATH, response_model=AgentEgressView)
    async def rules(request: Request) -> AgentEgressView:
        verified = await authenticate(request)
        return projection.for_caller(verified)

    return app
