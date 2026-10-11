"""Serve the real History Service over a history database for consumer tests.

The schema belongs to the Sandbox Service fixture that creates the database. `ingest` writes it as
production does, following that fixture's Sandbox Service; the server only reads it, in read-only
transactions as in production.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import cast

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from agentplane.grpc_serving import serving
from agentplane.history_service.channels import Channel, create_wakeups
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.evidence import EvidenceReader
from agentplane.history_service.fold_runner import FoldRunner
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.ingester import HistoryIngester
from agentplane.history_service.storage.store import Store
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.thread_reads import ThreadReader
from agentplane.postgres.wakeups import notify
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import POD_NAME_CLAIM, POD_UID_CLAIM, WorkloadPrincipalResolver
from util.testing.sqlalchemy_engines import managed_async_engine

# The asyncpg dialect is loaded by SQLAlchemy from the driver name.
# gazelle:include_dep @pypi//asyncpg

TOKEN = "test-app-history-service-token"  # a test literal, not a real credential
AUDIENCE = "test-app-history-service"
READER = ServiceAccountRef(namespace="test-history-namespace", name="test-history-reader")


class Authentication:
    async def create_token_review(self, body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        return k8s_client.V1TokenReview(
            spec=body.spec,
            status=k8s_client.V1TokenReviewStatus(
                authenticated=body.spec.token == TOKEN,
                audiences=[AUDIENCE],
                user=k8s_client.V1UserInfo(
                    username=f"system:serviceaccount:{READER.namespace}:{READER.name}",
                    uid="test-app-account-uid",
                    extra={POD_NAME_CLAIM: ["test-app-pod"], POD_UID_CLAIM: ["test-app-pod-uid"]},
                ),
            ),
        )


@asynccontextmanager
async def _history_engine(database_url: str, *, read_only: bool = False) -> AsyncIterator[AsyncEngine]:
    """Own a History test engine, enforcing read-only transactions for the reader."""
    async with managed_async_engine(
        create_async_engine(
            make_url(database_url).set(drivername="postgresql+asyncpg"),
            connect_args={"server_settings": {"default_transaction_read_only": "on"}} if read_only else {},
        )
    ) as engine:
        yield engine


@asynccontextmanager
async def history_service(
    database_url: str, token_file: Path, *, watch_lease_s: float = 300
) -> AsyncIterator[HistoryServiceClient]:
    async with _history_engine(database_url, read_only=True) as engine:
        history = Store(engine)
        wakeups = create_wakeups(make_url(database_url))
        server = grpc.aio.server()
        add_service(
            Resources(
                principals=WorkloadPrincipalResolver(
                    authentication=cast(k8s_client.AuthenticationV1Api, Authentication()),
                    audience=AUDIENCE,
                    allowed_service_account_namespaces={READER.namespace},
                ),
                history=history,
                summaries=SummaryReader(engine),
                threads=ThreadReader(engine),
                evidence=EvidenceReader(engine, history),
                reader_accounts=frozenset({READER}),
                request_timeout_s=10,
                summary_changes=wakeups.changes[Channel.SUMMARIES],
                watch_lease_s=watch_lease_s,
            ),
            server,
        )
        await asyncio.to_thread(token_file.write_text, TOKEN)
        async with (
            serving(server, "127.0.0.1:0", grace_s=0) as port,
            HistoryServiceClient(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=20) as client,
            wakeups.listener.listen(),
        ):
            yield client


@asynccontextmanager
async def ingest(database_url: str, sandboxes: SandboxServiceClient) -> AsyncIterator[None]:
    """Copy every Session `sandboxes` reports into the history database, as the deployed ingester does."""
    async with _history_engine(database_url) as engine:
        task = asyncio.create_task(
            HistoryIngester(Store(engine), engine, sandboxes, concurrency=4, retry_interval_s=0.05).run()
        )
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def fold_history(database_url: str) -> None:
    """Fold every retained Session in the history database until the History Service fold has caught up.

    The composed History + Commands test database template already contains the projection tables.
    """
    async with _history_engine(database_url) as engine:
        folder = FoldRunner(engine=engine, history=Store(engine), concurrency=1, page_size=1000)
        try:
            while (await folder.cycle()).behind:
                pass
        finally:
            await folder.release()


async def announce_fold_commit(database_url: str) -> None:
    """Commit the notice a fold commit sends, without folding anything."""
    async with (
        _history_engine(database_url) as engine,
        engine.begin() as connection,
        AsyncSession(bind=connection) as session,
    ):
        await notify(session, Channel.SUMMARIES)
