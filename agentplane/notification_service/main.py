"""Standalone notification API and workers. All dependencies are independently owned backends."""

import asyncio

import httpx
import uvicorn
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.notification_service.api import create_app
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import Settings
from agentplane.notification_service.sources.actions import Actions
from agentplane.notification_service.sources.github import GitHub
from agentplane.notification_service.store import Store
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# gazelle:include_dep @pypi//asyncpg


async def serve(settings: Settings) -> None:
    k8s_config.load_incluster_config()
    # Staging OOM: 22 notification sessions, 21 idle across two replicas.
    engine = create_async_engine(
        make_url(settings.database_url).set(drivername="postgresql+asyncpg"), pool_size=4, max_overflow=2
    )
    sandboxes = SandboxServiceClient(
        settings.sandbox_service.target,
        namespace=settings.namespace,
        token_file=settings.sandbox_service.token_file,
        request_timeout_s=5,
    )
    try:
        async with (
            k8s_client.ApiClient() as kube,
            httpx.AsyncClient(base_url=settings.actions.url, timeout=5, follow_redirects=False) as http,
            httpx.AsyncClient(base_url="https://api.github.com", timeout=5, follow_redirects=False) as github_http,
        ):
            principals = WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(kube),
                audience=settings.token_audience,
                allowed_service_account_namespaces={settings.namespace},
            )
            github = GitHub(github_http, settings.github) if settings.github is not None else None
            if github is not None:
                github.start()
            app = create_app(
                Service(
                    Store(engine),
                    Actions(http, settings.actions.token_file),
                    sandboxes,
                    github,
                    notice_debounce=settings.notice_debounce,
                ),
                principals,
            )
            await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()
    finally:
        await sandboxes.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
