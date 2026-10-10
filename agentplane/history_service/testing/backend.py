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
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.sandbox_service.session_history.store import Store
from agentplane.subjects import ServiceAccountRef
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
async def history_service(database_url: str, token_file: Path) -> AsyncIterator[HistoryServiceClient]:
    engine = create_async_engine(
        make_url(database_url).set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )
    server = grpc.aio.server()
    add_service(
        Resources(
            principals=WorkloadPrincipalResolver(
                authentication=cast(k8s_client.AuthenticationV1Api, Authentication()),
                audience=AUDIENCE,
                allowed_service_account_namespaces={READER.namespace},
            ),
            history=Store(engine),
            reader_accounts=frozenset({READER}),
            request_timeout_s=10,
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await asyncio.to_thread(token_file.write_text, TOKEN)
    client = HistoryServiceClient(f"127.0.0.1:{port}", token_file=token_file, request_timeout_s=20)
    await server.start()
    try:
        yield client
    finally:
        await client.close()
        await server.stop(0)
        await engine.dispose()
