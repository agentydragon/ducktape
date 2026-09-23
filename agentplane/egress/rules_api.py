"""Destination-authenticated HTTP API for a Sandbox's redacted effective egress rules."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, Request

from agentplane.egress.agent_view import AgentEgressView, agent_view
from agentplane.egress.policy import Index
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from agentplane.workload_auth.principal import WorkloadPrincipal

HOST = "agentplane-egress.agentplane-staging.svc.cluster.local"
PATH = "/v1/rules"
URL = f"http://{HOST}{PATH}"


class RulesProjection:
    """Project one caller's effective rules without exposing source resources."""

    def __init__(self, index: Index, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._index = index
        self._clock = clock

    def for_caller(self, principal: WorkloadPrincipal) -> AgentEgressView:
        """The view of the ServiceAccount the bearer proved. Nothing here can go stale between
        authentication and projection: the subject is the token's, not an object in the index."""
        return agent_view(
            self._index,
            ServiceAccountRef(namespace=principal.namespace, name=principal.service_account_name),
            self._clock(),
        )


def create_rules_app(authenticate: WorkloadPrincipalAuthenticator, projection: RulesProjection) -> FastAPI:
    """Create the ordinary destination API; request metadata is never an identity authority."""
    app = FastAPI(title="agentplane-egress-rules")

    @app.get(PATH, response_model=AgentEgressView)
    async def rules(request: Request) -> AgentEgressView:
        verified = await authenticate(request)
        return projection.for_caller(verified)

    return app


class _EmbeddedServer(uvicorn.Server):
    """Uvicorn hosted inside the proxy process, whose outer lifecycle owns signal handling."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@asynccontextmanager
async def serve_rules_api(app: FastAPI, host: str, port: int) -> AsyncIterator[None]:
    """Run the agent API alongside mitmproxy until the central process shuts down."""
    server = _EmbeddedServer(uvicorn.Config(app, host=host, port=port, access_log=False, timeout_graceful_shutdown=5))
    task = asyncio.create_task(server.serve(), name="egress-rules-api")
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("rules API exited before accepting connections")
            await asyncio.sleep(0.01)
        yield
    finally:
        server.should_exit = True
        await task
