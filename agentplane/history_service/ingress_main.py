"""Runner ingress entry point: commits the journals runners push, apart from the History Service's reads."""

import asyncio
import logging

import grpc
import uvicorn
from fastapi import FastAPI
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.history_service.ingress import Resources, Timing, add_ingress
from agentplane.history_service.ingress_settings import IngressSettings
from agentplane.sandbox_service.session_history.store import Store
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg


async def serve(settings: IngressSettings) -> None:
    if settings.port == settings.health_port:
        raise ValueError("gRPC and health ports must differ")
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    engine = create_async_engine(
        make_url(settings.database_url).set(drivername="postgresql+asyncpg"), pool_size=8, max_overflow=4
    )
    try:
        async with k8s_client.ApiClient(configuration) as api:
            server = grpc.aio.server()
            add_ingress(
                Resources(
                    principals=WorkloadPrincipalResolver(
                        authentication=k8s_client.AuthenticationV1Api(api),
                        audience=settings.token_audience,
                        allowed_service_account_namespaces=settings.writer_namespaces,
                    ),
                    history=Store(engine, settle_deltas=settings.settle_deltas),
                    timing=Timing(),
                ),
                server,
            )
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
    asyncio.run(serve(IngressSettings()))


if __name__ == "__main__":
    main()
