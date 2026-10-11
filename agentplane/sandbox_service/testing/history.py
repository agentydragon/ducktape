"""One service database for cross-service/native integration tests, and the Sessions a test attaches
on a runner directly.

A runner Session a test attaches directly, bypassing CreateSession, is registered here under its
own UUID, so the History Service ingester under test follows it like any other.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import grpc
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.history_service.storage.db import Base
from agentplane.runner.client import RunnerClient
from agentplane.runner_gateway.commands import db as commands_db
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.runner_gateway.testing.sessions import register_session
from agentplane.sandbox_service.destinations import DestinationUnavailableError
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//protobuf


@asynccontextmanager
async def service_database(database_url: str) -> AsyncIterator[AsyncEngine]:
    """One test database for the history and Sandbox Service commands schemas."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            # Production keeps admission in its own database; one test database serves both here.
            await connection.run_sync(commands_db.Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def register_attached_sessions(resources: Resources, engine: AsyncEngine) -> AsyncIterator[None]:
    sessions = resources.sessions

    async def discover() -> None:
        while True:
            for view in await resources.destinations.inventory.list_sandboxes():
                try:
                    destination = SandboxDestination(
                        sandbox=view.name, sandbox_uid=view.uid, owner=view.service_account
                    )
                    endpoint = await resources.destinations.resolve(destination, writes=False)
                    runner = RunnerClient(grpc.aio.insecure_channel(endpoint.target))
                    try:
                        summaries = await runner.list_sessions()
                    finally:
                        await runner.close()
                    namespace = resources.destinations.inventory.namespace
                    for summary in summaries:
                        # CreateSession registered its own; a runner Session a test attached directly
                        # is named by its UUID.
                        if (
                            await sessions.session_id(
                                sandbox_namespace=namespace,
                                sandbox_name=view.name,
                                sandbox_uid=UUID(view.uid),
                                runner_session_id=summary.session_id,
                            )
                            is None
                        ):
                            await register_session(
                                engine,
                                UUID(summary.session_id),
                                sandbox_namespace=namespace,
                                sandbox_name=view.name,
                                sandbox_uid=UUID(view.uid),
                                runner_session_id=summary.session_id,
                            )
                except SandboxNotFoundError, DestinationUnavailableError, grpc.RpcError:
                    continue
            await asyncio.sleep(0.05)

    task = asyncio.create_task(discover())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
