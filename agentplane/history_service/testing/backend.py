"""Serve the real History Service read boundary over a history database for consumer tests.

The schema and rows belong to whatever writes that database in the test (the Sandbox Service
fixture's ingester); this server only reads it, in read-only transactions as in production.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.evidence import EvidenceReader
from agentplane.history_service.fold_shadow import FoldShadow
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.summary_changes import SummaryChanges, notify_summaries
from agentplane.history_service.thread_reads import ThreadReader
from agentplane.sandbox_service.session_history.store import Store
from agentplane.subjects import ServiceAccountRef
from agentplane.thread_fold.models import Base
from agentplane.workload_auth.principal import POD_NAME_CLAIM, POD_UID_CLAIM, WorkloadPrincipalResolver

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
                    extra={POD_NAME_CLAIM: ["test-app-pod"], POD_UID_CLAIM: ["test-app-pod-uid"]},
                ),
            ),
        )


@asynccontextmanager
async def history_service(
    database_url: str, token_file: Path, *, watch_lease_s: float = 300
) -> AsyncIterator[HistoryServiceClient]:
    engine = create_async_engine(
        make_url(database_url).set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )
    history = Store(engine)
    summary_changes = SummaryChanges(make_url(database_url))
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
            summary_changes=summary_changes,
            watch_lease_s=watch_lease_s,
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await asyncio.to_thread(token_file.write_text, TOKEN)
    client = HistoryServiceClient(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=20)
    await server.start()
    try:
        async with summary_changes.listener.listen():
            yield client
    finally:
        await client.close()
        await server.stop(0)
        await engine.dispose()


async def fold_history(database_url: str) -> None:
    """Fold every retained Session in the history database until the History Service fold has caught up.

    The fold is this database's writer of the projection tables, so it creates them as migrations would.
    """
    engine = create_async_engine(make_url(database_url).set(drivername="postgresql+asyncpg"))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        shadow = FoldShadow(engine=engine, history=Store(engine), concurrency=1)
        try:
            while (await shadow.cycle()).behind:
                pass
        finally:
            await shadow.release()
    finally:
        await engine.dispose()


async def announce_fold_commit(database_url: str) -> None:
    """Commit the notice a fold commit sends, without folding anything."""
    engine = create_async_engine(make_url(database_url).set(drivername="postgresql+asyncpg"))
    try:
        async with engine.begin() as connection, AsyncSession(bind=connection) as session:
            await notify_summaries(session)
    finally:
        await engine.dispose()
