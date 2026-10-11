"""History Service entry point: takes the journals runners push, serves retained Session history and folds it."""

import asyncio

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.grpc_serving import add_health, serving
from agentplane.history_service import ingress
from agentplane.history_service.channels import Channel, create_wakeups
from agentplane.history_service.evidence import EvidenceReader
from agentplane.history_service.fold_runner import FoldRunner
from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.settings import Settings
from agentplane.history_service.storage.store import Store
from agentplane.history_service.summaries import SummaryReader
from agentplane.history_service.thread_reads import ThreadReader
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg


async def serve(settings: Settings) -> None:
    url = make_url(settings.database_url).set(drivername="postgresql+asyncpg")
    # The migration init container creates these tables; the ingress and the fold write through
    # their own engines, and reads never write.
    engine = create_async_engine(
        url, pool_size=4, max_overflow=2, connect_args={"server_settings": {"default_transaction_read_only": "on"}}
    )
    # The fold writes only its own tables.
    fold_engine = create_async_engine(url, pool_size=2, max_overflow=2)
    ingress_engine = create_async_engine(url, pool_size=8, max_overflow=4)
    wakeups = create_wakeups(url)
    try:
        async with (
            k8s_client.ApiClient(await kubernetes_configuration(settings.kubeconfig)) as api,
            wakeups.listener.listen(),
        ):
            history = Store(engine)
            server = grpc.aio.server()
            health = add_health(server)
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
                summary_changes=wakeups.changes[Channel.SUMMARIES],
                watch_lease_s=settings.watch_lease_s,
            )
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
            async with (
                background(
                    FoldRunner(
                        engine=fold_engine,
                        history=history,
                        concurrency=settings.fold_concurrency,
                        page_size=settings.fold_page_size,
                    ).run(interval_s=settings.fold_interval_s, changes=wakeups.changes[Channel.HISTORY], health=health),
                    name="thread-fold",
                ),
                serving(ingress_server, f"{settings.host}:{settings.ingress.port}", grace_s=5),
                serving(server, f"{settings.host}:{settings.port}", grace_s=5),
            ):
                await until_terminated()
    finally:
        await ingress_engine.dispose()
        await fold_engine.dispose()
        await engine.dispose()


def main() -> None:
    configure_logging()
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
