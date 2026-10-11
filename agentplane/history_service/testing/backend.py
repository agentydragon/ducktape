"""Serve the real History Service over a history database for consumer tests.

The schema belongs to the Sandbox Service fixture that creates the database. `ingest` writes it as
production does, a runner pushing to the ingress; the server only reads it, in read-only
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
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.evidence import EvidenceReader
from agentplane.history_service.fold_shadow import FoldShadow
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.ingress import Resources as IngressResources, Timing, add_ingress
from agentplane.history_service.storage.history_changes import HistoryChanges, notify_history
from agentplane.history_service.storage.store import Store
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.summary_changes import SummaryChanges, notify_summaries
from agentplane.history_service.thread_reads import ThreadReader
from agentplane.runner.control import ControlTiming
from agentplane.runner.ingress import IngressPusher
from agentplane.runner.service import Runner
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import POD_NAME_CLAIM, POD_UID_CLAIM, WorkloadPrincipalResolver
from util.testing.sqlalchemy_engines import managed_async_engine

# The asyncpg dialect is loaded by SQLAlchemy from the driver name.
# gazelle:include_dep @pypi//asyncpg

TOKEN = "test-app-history-service-token"  # a test literal, not a real credential
AUDIENCE = "test-app-history-service"
READER = ServiceAccountRef(namespace="test-history-namespace", name="test-history-reader")
WRITER_TOKEN = "test-runner-ingress-token"  # a test literal, not a real credential
WRITER_AUDIENCE = "test-runner-ingress"
WRITER = ServiceAccountRef(namespace="test-sandbox-namespace", name="test-sandbox-runner")


class Authentication:
    """Token reviews for one ServiceAccount, presenting one token for one audience."""

    def __init__(self, *, token: str, audience: str, account: ServiceAccountRef) -> None:
        self.token = token
        self.audience = audience
        self.account = account

    async def create_token_review(self, body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        return k8s_client.V1TokenReview(
            spec=body.spec,
            status=k8s_client.V1TokenReviewStatus(
                authenticated=body.spec.token == self.token,
                audiences=[self.audience],
                user=k8s_client.V1UserInfo(
                    username=f"system:serviceaccount:{self.account.namespace}:{self.account.name}",
                    uid=f"{self.account.name}-uid",
                    extra={
                        POD_NAME_CLAIM: [f"{self.account.name}-pod"],
                        POD_UID_CLAIM: [f"{self.account.name}-pod-uid"],
                    },
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
        history_changes = HistoryChanges(make_url(database_url))
        summary_changes = SummaryChanges(make_url(database_url))
        server = grpc.aio.server()
        add_service(
            Resources(
                principals=WorkloadPrincipalResolver(
                    authentication=cast(
                        k8s_client.AuthenticationV1Api, Authentication(token=TOKEN, audience=AUDIENCE, account=READER)
                    ),
                    audience=AUDIENCE,
                    allowed_service_account_namespaces={READER.namespace},
                ),
                history=history,
                summaries=SummaryReader(engine),
                threads=ThreadReader(engine),
                evidence=EvidenceReader(engine, history),
                reader_accounts=frozenset({READER}),
                request_timeout_s=10,
                history_changes=history_changes,
                summary_changes=summary_changes,
                watch_lease_s=watch_lease_s,
            ),
            server,
        )
        await asyncio.to_thread(token_file.write_text, TOKEN)
        async with (
            serving(server, "127.0.0.1:0", grace_s=0) as port,
            HistoryServiceClient(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=20) as client,
            history_changes.listener.listen(),
            summary_changes.listener.listen(),
        ):
            yield client


@asynccontextmanager
async def ingest(database_url: str, runner: Runner) -> AsyncIterator[None]:
    """Push every Session `runner` journals into the history database through the ingress, as a
    deployed runner does."""
    async with _history_engine(database_url) as engine:
        server = grpc.aio.server()
        add_ingress(
            IngressResources(
                principals=WorkloadPrincipalResolver(
                    authentication=cast(
                        k8s_client.AuthenticationV1Api,
                        Authentication(token=WRITER_TOKEN, audience=WRITER_AUDIENCE, account=WRITER),
                    ),
                    audience=WRITER_AUDIENCE,
                    allowed_service_account_namespaces={WRITER.namespace},
                ),
                history=Store(engine),
                timing=Timing(),
            ),
            server,
        )
        async with serving(server, "127.0.0.1:0", grace_s=0) as port:
            task = asyncio.create_task(
                IngressPusher(
                    runner,
                    target=f"127.0.0.1:{port}",
                    bearer=WRITER_TOKEN,
                    timing=ControlTiming(backoff_initial_s=0.05, backoff_max_s=0.5),
                ).run()
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
        shadow = FoldShadow(engine=engine, history=Store(engine), concurrency=1, page_size=1000)
        try:
            while (await shadow.cycle()).behind:
                pass
        finally:
            await shadow.release()


async def announce_fold_commit(database_url: str) -> None:
    """Commit the notice a fold commit sends, without folding anything."""
    async with (
        _history_engine(database_url) as engine,
        engine.begin() as connection,
        AsyncSession(bind=connection) as session,
    ):
        await notify_summaries(session)


async def announce_history_change(database_url: str) -> None:
    """Commit a raw-history invalidation for app watch tests."""
    async with (
        _history_engine(database_url) as engine,
        engine.begin() as connection,
        AsyncSession(bind=connection) as session,
    ):
        await notify_history(session)
