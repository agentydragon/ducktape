"""Standalone notification API and workers. All dependencies are independently owned backends."""

import asyncio
from contextlib import AsyncExitStack

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
from agentplane.notification_service.sources.github_client import GitHubClient
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
        request_timeout_s=settings.sandbox_service.request_timeout_s,
        lifecycle_timeout_s=settings.sandbox_service.lifecycle_timeout_s,
        follow_timeout_s=settings.sandbox_service.follow_timeout_s,
        command_admission_timeout_s=settings.sandbox_service.command_admission_timeout_s,
        channel_options=settings.sandbox_service.grpc_channel_options,
    )
    try:
        async with (
            k8s_client.ApiClient() as kube,
            httpx.AsyncClient(base_url=str(settings.actions.url), timeout=5, follow_redirects=False) as http,
            AsyncExitStack() as github_stack,
        ):
            principals = WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(kube),
                audience=settings.token_audience,
                allowed_service_account_namespaces={settings.namespace},
            )
            github = None
            if settings.github is not None:
                client = await github_stack.enter_async_context(GitHubClient.open(settings.github))
                github = GitHub(client)
            app = create_app(
                Service(
                    Store(engine, quotas=settings.quotas),
                    Actions(http, settings.actions.token_file),
                    sandboxes,
                    github,
                    notice_debounce=settings.notice_debounce,
                    stale_confirmation_s=settings.stale_inbox_confirmation_s,
                    operator_reader_account=settings.operator_reader_account,
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
