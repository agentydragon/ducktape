"""Register Sessions that `CreateSession` did not: with a chosen public ID and runner locator and no Open
reservation, as Sessions registered before `CreateSession` existed are, or attached directly to a
runner by a test."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import grpc
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.proto.sandbox_service_pb2 import SandboxDestination
from agentplane.runner.client import RunnerClient
from agentplane.runner_gateway.commands.db import Session
from agentplane.runner_gateway.grpc_api import Resources
from agentplane.sandbox_service.destinations import DestinationUnavailableError
from agentplane.sandbox_service.models import SandboxNotFoundError

# gazelle:include_dep @pypi//protobuf


async def register_session(
    engine: AsyncEngine,
    session_id: UUID,
    *,
    sandbox_namespace: str,
    sandbox_name: str,
    sandbox_uid: UUID | None,
    runner_session_id: str,
) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False).begin() as session:
        session.add(
            Session(
                id=session_id,
                sandbox_namespace=sandbox_namespace,
                sandbox_name=sandbox_name,
                sandbox_uid=sandbox_uid,
                runner_session_id=runner_session_id,
            )
        )


@asynccontextmanager
async def register_attached_sessions(resources: Resources, engine: AsyncEngine) -> AsyncIterator[None]:
    """Register every runner Session a test attached directly, bypassing CreateSession, under its own
    UUID, so the Session registry names it like any other."""
    sessions = resources.sessions

    async def discover() -> None:
        while True:
            for view in await resources.destinations.inventory.list_sandboxes():
                try:
                    destination = SandboxDestination(
                        sandbox=view.name, sandbox_uid=view.uid, owner=view.service_account
                    )
                    endpoint = await resources.destinations.resolve(destination, writes=False)
                    async with RunnerClient(endpoint.target, channel_options=None) as runner:
                        summaries = await runner.list_sessions()
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
