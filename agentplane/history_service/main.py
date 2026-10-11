"""History Service entry point: takes the journals runners push, serves retained Session history and folds it."""

import asyncio
import contextlib
import logging

import grpc
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.grpc_process import until_terminated
from agentplane.grpc_serving import add_health, serving
from agentplane.history_service import ingress
from agentplane.history_service.evidence import EvidenceReader
from agentplane.history_service.fold_shadow import FoldShadow
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.settings import Settings
from agentplane.history_service.storage.history_changes import HistoryChanges
from agentplane.history_service.storage.store import Store
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.summary_changes import SummaryChanges
from agentplane.history_service.thread_reads import ThreadReader
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg

logger = logging.getLogger(__name__)


async def serve(settings: Settings) -> None:
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    url = make_url(settings.database_url).set(drivername="postgresql+asyncpg")
    # The migration init container owns the schema; the ingress and the fold write through their own
    # engines, and reads never write.
    engine = create_async_engine(
        url, pool_size=4, max_overflow=2, connect_args={"server_settings": {"default_transaction_read_only": "on"}}
    )
    # The fold writes only its own tables.
    fold_engine = create_async_engine(url, pool_size=2, max_overflow=2)
    ingress_engine = create_async_engine(url, pool_size=8, max_overflow=4)
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
                    FoldShadow(
                        engine=fold_engine,
                        history=history,
                        concurrency=settings.fold_concurrency,
                        page_size=settings.fold_page_size,
                    ).run(interval_s=settings.fold_interval_s, changes=history_changes),
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
            add_health(server)
            add_service(resources, server)
            if settings.port == settings.ingress.port:
                raise ValueError("gRPC and runner ingress ports must differ")
            # A separate listener, so the network fence can admit runners to the ingress and nothing else.
            ingress_server = grpc.aio.server()
            ingress.add_ingress(
                ingress.Resources(
                    principals=WorkloadPrincipalResolver(
                        authentication=k8s_client.AuthenticationV1Api(api),
                        audience=settings.ingress.token_audience,
                        allowed_service_account_namespaces=settings.ingress.writer_namespaces,
                    ),
                    history=Store(ingress_engine, settle_deltas=settings.ingress.settle_deltas),
                    timing=ingress.Timing(),
                ),
                ingress_server,
            )
            try:
                async with (
                    serving(ingress_server, f"{settings.host}:{settings.ingress.port}", grace_s=5),
                    serving(server, f"{settings.host}:{settings.port}", grace_s=5),
                ):
                    await until_terminated()
            finally:
                if shadow is not None:
                    shadow.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await shadow
    finally:
        await ingress_engine.dispose()
        await fold_engine.dispose()
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
