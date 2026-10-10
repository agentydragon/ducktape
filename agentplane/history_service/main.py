"""Standalone History Service entry point: serves retained Session history reads."""

import asyncio
import logging

import grpc
import uvicorn
from fastapi import FastAPI
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.history_service.grpc_api import Resources, add_service
from agentplane.history_service.settings import Settings
from agentplane.sandbox_service.session_history.store import Store
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg


async def serve(settings: Settings) -> None:
    if settings.port == settings.health_port:
        raise ValueError("gRPC and health ports must differ")
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    # The Sandbox Service still writes and migrates these tables; until the History Service takes
    # over writing, its connections refuse writes.
    engine = create_async_engine(
        make_url(settings.database_url).set(drivername="postgresql+asyncpg"),
        pool_size=4,
        max_overflow=2,
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )
    try:
        async with k8s_client.ApiClient(configuration) as api:
            resources = Resources(
                principals=WorkloadPrincipalResolver(
                    authentication=k8s_client.AuthenticationV1Api(api),
                    audience=settings.token_audience,
                    allowed_service_account_namespaces={account.namespace for account in settings.reader_accounts},
                ),
                history=Store(engine),
                reader_accounts=settings.reader_accounts,
                request_timeout_s=settings.request_timeout_s,
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
                await uvicorn.Server(
                    uvicorn.Config(health, host=settings.host, port=settings.health_port, access_log=False)
                ).serve()
            finally:
                await server.stop(grace=5)
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # TokenReview responses echo their bearer. Do not log the generated client's wire bodies.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
