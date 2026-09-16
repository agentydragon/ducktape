"""Destination-authenticated HTTP API for a Sandbox's redacted effective egress rules."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status

from x.agentplane.egress.agent_view import AgentEgressView, agent_view
from x.agentplane.egress.policy import Index, SandboxCaller, ServiceAccountCaller
from x.agentplane.sandbox_auth.http import WorkloadPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipal, WorkloadPrincipal

HOST = "agentplane-egress.agentplane-staging.svc.cluster.local"
PATH = "/v1/rules"
URL = f"http://{HOST}{PATH}"


class SandboxNotCurrentError(Exception):
    """The authenticated Sandbox no longer exists under the same UID in the policy index.

    Only a Sandbox caller can hit this: a ServiceAccount subject has no index object to go stale.
    """


class RulesProjection:
    """Project one caller's effective rules without exposing source resources."""

    def __init__(self, index: Index, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._index = index
        self._clock = clock

    def for_caller(self, principal: WorkloadPrincipal) -> AgentEgressView:
        """The view of whichever subject the bearer proved.

        A Sandbox principal must still be in the index under the same UID, as the decision path
        requires; a workload principal is its ServiceAccount, and there is nothing to re-check.
        """
        if not isinstance(principal, SandboxPrincipal):
            return agent_view(
                self._index,
                ServiceAccountCaller(
                    namespace=principal.namespace, service_account_name=principal.service_account_name
                ),
                self._clock(),
            )
        sandbox = self._index.sandboxes.get(principal.sandbox_name)
        if sandbox is None or sandbox.metadata.uid != principal.sandbox_uid:
            raise SandboxNotCurrentError(principal.sandbox_name)
        return agent_view(self._index, SandboxCaller(sandbox), self._clock())


def create_rules_app(authenticate: WorkloadPrincipalAuthenticator, projection: RulesProjection) -> FastAPI:
    """Create the ordinary destination API; request metadata is never an identity authority."""
    app = FastAPI(title="agentplane-egress-rules")

    @app.get(PATH, response_model=AgentEgressView)
    async def rules(request: Request) -> AgentEgressView:
        verified = await authenticate(request)
        try:
            return projection.for_caller(verified)
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
