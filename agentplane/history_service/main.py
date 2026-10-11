"""Standalone History Service entry point: serves retained Session history and, when enabled, ingests it."""

import asyncio
import contextlib
import logging

import grpc
import uvicorn
from fastapi import FastAPI
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.history_service.evidence import EvidenceReader
from agentplane.history_service.fold_shadow import FoldShadow
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.ingester import HistoryIngester
from agentplane.history_service.settings import Settings
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.summary_changes import SummaryChanges
from agentplane.history_service.thread_reads import ThreadReader
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.session_history.history_changes import HistoryChanges
from agentplane.sandbox_service.session_history.store import Store
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg

logger = logging.getLogger(__name__)


async def serve(settings: Settings) -> None:
    if settings.port == settings.health_port:
        raise ValueError("gRPC and health ports must differ")
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    url = make_url(settings.database_url).set(drivername="postgresql+asyncpg")
    # The Sandbox Service's init container migrates these tables; the ingester and the fold write
    # through their own engines, and reads never write.
    engine = create_async_engine(
        url, pool_size=4, max_overflow=2, connect_args={"server_settings": {"default_transaction_read_only": "on"}}
    )
    # The fold writes only its own tables.
    fold_engine = create_async_engine(url, pool_size=2, max_overflow=2)
    history_changes = HistoryChanges(url)
    summary_changes = SummaryChanges(url)
    try:
        async with (
            k8s_client.ApiClient(configuration) as api,
            history_changes.listener.listen(),
            summary_changes.listener.listen(),
        ):
            history = Store(engine)
            # A failed fold stops folding, not the raw reads this process serves.
            shadow = (
                asyncio.create_task(
                    FoldShadow(engine=fold_engine, history=history, concurrency=settings.fold_concurrency).run(
                        interval_s=settings.fold_interval_s, changes=history_changes
                    ),
                    name="fold-shadow",
                )
                if settings.fold_shadow
                else None
            )
            if shadow is not None:
                shadow.add_done_callback(_report_shadow_exit)
            resources = Resources(
                principals=WorkloadPrincipalResolver(
                    authentication=k8s_client.AuthenticationV1Api(api),
                    audience=settings.token_audience,
                    allowed_service_account_namespaces={account.namespace for account in settings.reader_accounts},
                ),
                history=history,
                summaries=SummaryReader(engine),
                threads=ThreadReader(engine),
                evidence=EvidenceReader(engine, history),
                reader_accounts=settings.reader_accounts,
                request_timeout_s=settings.request_timeout_s,
                history_changes=history_changes,
                summary_changes=summary_changes,
                watch_lease_s=settings.watch_lease_s,
            )
            server = grpc.aio.server()
            add_service(resources, server)
            server.add_insecure_port(f"{settings.host}:{settings.port}")
            await server.start()
            health = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

            @health.get("/healthz")
            async def healthz() -> dict[str, str]:
                return {"status": "ok"}

            try:
                # An ingester failure ends the process rather than leaving reads served with no writer.
                async with asyncio.TaskGroup() as tasks:
                    ingesting = (
                        tasks.create_task(ingest(settings, url), name="history-ingester")
                        if settings.ingester.enabled
                        else None
                    )
                    await uvicorn.Server(
                        uvicorn.Config(health, host=settings.host, port=settings.health_port, access_log=False)
                    ).serve()
                    if ingesting is not None:
                        ingesting.cancel()
            finally:
                await server.stop(grace=5)
                if shadow is not None:
                    shadow.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await shadow
    finally:
        await fold_engine.dispose()
        await engine.dispose()


async def ingest(settings: Settings, url: URL) -> None:
    # Each followed Session holds a claim connection and briefly a write connection.
    engine = create_async_engine(
        url, pool_size=settings.ingester.concurrency, max_overflow=settings.ingester.concurrency + 2
    )
    target = settings.sandbox_service
    sandboxes = SandboxServiceClient(
        target.target,
        gateway_target=target.gateway_target or target.target,
        namespace=None,  # the ingester follows the destinations the Session feed names
        token_file=target.token_file,
        command_admission_timeout_s=None,
        request_timeout_s=target.request_timeout_s,
        lifecycle_timeout_s=target.request_timeout_s,
        follow_timeout_s=target.follow_timeout_s,
        channel_options=target.grpc_channel_options,
    )
    try:
        await HistoryIngester(
            Store(engine, settle_deltas=settings.ingester.settle_deltas),
            engine,
            sandboxes,
            concurrency=settings.ingester.concurrency,
            retry_interval_s=settings.ingester.retry_interval_s,
        ).run()
    finally:
        await sandboxes.close()
        await engine.dispose()


def _report_shadow_exit(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("fold shadow stopped", exc_info=error)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # TokenReview responses echo their bearer. Do not log the generated client's wire bodies.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
