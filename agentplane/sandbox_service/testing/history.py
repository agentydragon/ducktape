"""Real service history for cross-service/native integration tests only.

Consumer tests see a gRPC endpoint, not the Store or a service database session.
Imported test runners predate service-owned Open, so this fixture registers their
locators; the production HistoryIngester owns copying and lifecycle evidence.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid5

import grpc
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.runner.client import RunnerClient
from agentplane.sandbox_service.destinations import DestinationUnavailableError
from agentplane.sandbox_service.grpc_api import Resources
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination
from agentplane.sandbox_service.session_history.db import Base
from agentplane.sandbox_service.session_history.ingestion import HistoryIngester
from agentplane.sandbox_service.session_history.store import Store

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//protobuf


@asynccontextmanager
async def with_history(resources: Resources, database_url: str | None) -> AsyncIterator[Resources]:
    if database_url is None:
        yield resources
        return
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store = Store(engine)
    ingester = HistoryIngester(store, resources.destinations, runner_grpc_channel_options={}, interval_s=0.05)

    async def discover_and_copy() -> None:
        while True:
            for view in await resources.destinations.inventory.list_sandboxes():
                try:
                    destination = SandboxDestination(
                        sandbox=view.name, sandbox_uid=view.uid, owner=view.service_account
                    )
                    endpoint = await resources.destinations.resolve(destination)
                    runner = RunnerClient(grpc.aio.insecure_channel(endpoint.target))
                    try:
                        summaries = await runner.list_sessions()
                    finally:
                        await runner.close()
                    for summary in summaries:
                        try:
                            session_id = UUID(summary.session_id)
                        except ValueError:
                            session_id = uuid5(NAMESPACE_URL, view.name + "/" + summary.session_id)
                        await store.open(
                            session_id,
                            sandbox_namespace=resources.destinations.inventory.namespace,
                            sandbox_name=view.name,
                            sandbox_uid=UUID(view.uid),
                            runner_session_id=summary.session_id,
                        )
                except SandboxNotFoundError, DestinationUnavailableError, grpc.RpcError:
                    continue
            await ingester.cycle()
            await asyncio.sleep(0.05)

    task = asyncio.create_task(discover_and_copy())
    try:
        yield replace(resources, history=store, history_reader_accounts=resources.caller_accounts)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await engine.dispose()
