"""Destination-authenticated HTTP API for a Sandbox's redacted effective egress rules."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status

from x.agentplane.egress.agent_view import AgentEgressView, agent_view
from x.agentplane.egress.policy import Index
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator

HOST = "agentplane-egress.agentplane-staging.svc.cluster.local"
PATH = "/v1/rules"
URL = f"http://{HOST}{PATH}"


class SandboxNotCurrentError(Exception):
    """The authenticated Sandbox no longer exists under the same UID in the policy index."""


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


def create_rules_app(authenticate: SandboxPrincipalAuthenticator, projection: RulesProjection) -> FastAPI:
    """Create the ordinary destination API; request metadata is never an identity authority."""
    app = FastAPI(title="agentplane-egress-rules")

    @app.get(PATH, response_model=AgentEgressView)
    async def rules(request: Request) -> AgentEgressView:
        verified = await authenticate(request)
        try:
            return projection.for_sandbox(verified.sandbox_name, verified.sandbox_uid)
        except SandboxNotCurrentError as error:
            # Match the authenticator's deliberately generic response. The object name and UID may
            # have changed after TokenReview/live Pod resolution; neither belongs in the response.
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid workload bearer", headers={"WWW-Authenticate": "Bearer"}
            ) from error

    return app


class _EmbeddedServer(uvicorn.Server):
    """Uvicorn hosted inside the proxy process, whose outer lifecycle owns signal handling."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@asynccontextmanager
async def serve_rules_api(app: FastAPI, host: str, port: int) -> AsyncIterator[None]:
    """Run the agent API alongside mitmproxy until the central process shuts down."""
    server = _EmbeddedServer(uvicorn.Config(app, host=host, port=port, access_log=False))
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
